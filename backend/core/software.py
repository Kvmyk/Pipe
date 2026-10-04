"""
Wiedza o oprogramowaniu z publicznych API bez klucza:

  eol()    endoflife.date — wydania produktu, koniec wsparcia, najnowsza wersja
  vulns()  OSV.dev — znane podatnosci dla pakietu w konkretnej wersji i ekosystemie
           (Debian:12, Ubuntu:22.04:LTS, Alpine:v3.20, PyPI, npm, Go, Maven...)

Zwracaja gotowy tekst dla modelu. Synchroniczne (urllib przez websearch.get_json) —
handler uruchamia je przez asyncio.to_thread. Bez importu `settings`.
"""

from __future__ import annotations

import difflib
import re
import urllib.parse
from datetime import date
from typing import Any

from backend.core.i18n import tr
from backend.core.websearch import WebError, get_json

EOL_API = "https://endoflife.date/api/v1/products/"
OSV_API = "https://api.osv.dev/v1/query"
MAX_VULNS = 25

_PRODUCT = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")


def _product_names() -> list[str]:
    data = get_json(EOL_API)
    names: list[str] = []
    for item in data.get("result") or []:
        names.append(str(item.get("name", "")))
        names += [str(a) for a in item.get("aliases") or []]
    return [n for n in names if n]


def _matches(release: str, version: str) -> bool:
    """Cykl `16` pasuje do wersji `16`, `16.4`, `16.4-1`; cykl `22.04` do `22.04.5`."""
    return version == release or version.startswith(release + ".") or version.startswith(release + "-")


def _release_line(release: dict[str, Any], today: date) -> str:
    name = str(release.get("label") or release.get("name") or "?")
    latest = (release.get("latest") or {}).get("name")
    eol = release.get("eolFrom")
    if release.get("isEol"):
        status = tr(f"WSPARCIE ZAKONCZONE ({eol})" if eol else "WSPARCIE ZAKONCZONE",
                    f"END OF LIFE ({eol})" if eol else "END OF LIFE")
    elif eol:
        try:
            days = (date.fromisoformat(str(eol)) - today).days
            left = tr(f", za {days} dni", f", in {days} days") if days <= 180 else ""
        except ValueError:
            left = ""
        status = tr(f"wspierane do {eol}{left}", f"supported until {eol}{left}")
    else:
        status = tr("wspierane (bez daty konca)", "supported (no end date)")
    parts = [f"{name}: {status}"]
    if release.get("isLts"):
        parts.append("LTS")
    if latest:
        parts.append(tr(f"najnowsza {latest}", f"latest {latest}"))
    if release.get("releaseDate"):
        parts.append(tr(f"wydane {release['releaseDate']}", f"released {release['releaseDate']}"))
    return " · ".join(parts)


def eol(product: str, version: str = "", today: date | None = None) -> str:
    """Koniec wsparcia produktu (i konkretnej wersji, jesli podana) z endoflife.date."""
    today = today or date.today()
    product = product.strip().lower().replace(" ", "-")
    version = version.strip().lstrip("vV")
    if not _PRODUCT.match(product):
        raise WebError(tr("Niepoprawna nazwa produktu.", "Invalid product name."))
    try:
        data = get_json(EOL_API + urllib.parse.quote(product) + "/")
    except WebError as exc:
        if "HTTP 404" not in str(exc):
            raise
        close = difflib.get_close_matches(product, _product_names(), n=6, cutoff=0.6)
        hint = tr(f" Podobne nazwy: {', '.join(close)}.", f" Similar names: {', '.join(close)}.") if close else ""
        return tr(f"endoflife.date nie zna produktu {product!r}.{hint}", f"endoflife.date does not know {product!r}.{hint}")
    result = data.get("result") or {}
    releases = result.get("releases") or []
    lines = [tr(f"endoflife.date: {result.get('label') or product} (stan na {today.isoformat()})",
                f"endoflife.date: {result.get('label') or product} (as of {today.isoformat()})")]
    if result.get("versionCommand"):
        lines.append(tr(f"Wersje sprawdzisz: {result['versionCommand']}", f"Check the version with: {result['versionCommand']}"))
    if version:
        hit = next((r for r in releases if _matches(str(r.get("name", "")), version)), None)
        if hit is None:
            lines.append(tr(f"Wersja {version}: brak pasujacego cyklu wydan.", f"Version {version}: no matching release cycle."))
        else:
            lines.append(tr(f"Wersja {version} -> ", f"Version {version} -> ") + _release_line(hit, today))
            latest = (hit.get("latest") or {}).get("name")
            if latest and latest != version and version.count(".") >= str(latest).count("."):
                lines.append(tr(f"W tym cyklu jest nowsza wersja: {latest}.", f"There is a newer version in this cycle: {latest}."))
    lines.append(tr("Wydania (od najnowszego):", "Releases (newest first):"))
    lines += [f"- {_release_line(r, today)}" for r in releases[:8]]
    if len(releases) > 8:
        lines.append(tr(f"(i {len(releases) - 8} starszych)", f"(and {len(releases) - 8} older)"))
    return "\n".join(lines)


def _fixed_in(vuln: dict[str, Any], package: str, ecosystem: str) -> str:
    for affected in vuln.get("affected") or []:
        pkg = affected.get("package") or {}
        if pkg.get("name") != package or not str(pkg.get("ecosystem", "")).startswith(ecosystem.split(":")[0]):
            continue
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                if event.get("fixed"):
                    return str(event["fixed"])
    return ""


_NO_SEVERITY = {"not yet assigned", "unknown", ""}


def _severity(vuln: dict[str, Any]) -> str:
    value = _raw_severity(vuln)
    return "" if value in _NO_SEVERITY else value


def _raw_severity(vuln: dict[str, Any]) -> str:
    for item in vuln.get("severity") or []:
        score = str(item.get("score", ""))
        if score and not score.startswith("CVSS:"):
            return score.lower()
    specific = vuln.get("database_specific") or {}
    for key in ("severity", "urgency"):
        if specific.get(key):
            return str(specific[key]).lower()
    for affected in vuln.get("affected") or []:
        for key in ("severity", "urgency"):
            value = (affected.get("ecosystem_specific") or {}).get(key) or (affected.get("database_specific") or {}).get(key)
            if value:
                return str(value).lower()
    return ""


def vulns(ecosystem: str, package: str, version: str) -> str:
    """Znane podatnosci pakietu w danej wersji (OSV.dev). Najpierw te z poprawka — da sie je usunac aktualizacja."""
    ecosystem, package, version = ecosystem.strip(), package.strip(), version.strip()
    if not (ecosystem and package and version):
        raise WebError(tr("Podaj ecosystem, package i version.", "Give ecosystem, package and version."))
    data = get_json(OSV_API, {"package": {"name": package, "ecosystem": ecosystem}, "version": version}, timeout=30)
    found = data.get("vulns") or []
    head = f"OSV.dev: {package} {version} ({ecosystem})"
    if not found:
        return head + "\n" + tr(
            "Brak znanych podatnosci dla tej wersji. Uwaga: dla Debiana i Ubuntu podaj nazwe pakietu ZRODLOWEGO "
            "i pelna wersje pakietu (np. 1.22.1-9+deb12u2) — inaczej OSV moze nic nie znalezc.",
            "No known vulnerabilities for this version. Note: for Debian and Ubuntu give the SOURCE package name "
            "and the full package version (e.g. 1.22.1-9+deb12u2) — otherwise OSV may find nothing.")
    rows = []
    for vuln in found:
        fixed = _fixed_in(vuln, package, ecosystem)
        aliases = [a for a in vuln.get("aliases") or [] if a.startswith("CVE-")]
        summary = str(vuln.get("summary") or vuln.get("details") or "").strip().split("\n")[0]
        rows.append((not fixed, str(vuln.get("id", "")), aliases, _severity(vuln), fixed, summary[:180]))
    rows.sort(key=lambda r: (r[0], r[1]))
    fixable = sum(1 for r in rows if not r[0])
    lines = [head, tr(f"Podatnosci: {len(rows)}, z czego {fixable} ma poprawke w nowszej wersji pakietu.",
                      f"Vulnerabilities: {len(rows)}, {fixable} of them fixed in a newer package version.")]
    for unfixed, vid, aliases, severity, fixed, summary in rows[:MAX_VULNS]:
        ids = vid + (f" ({', '.join(aliases[:2])})" if aliases and aliases[0] not in vid else "")
        state = tr(f"poprawka: {fixed}", f"fixed in: {fixed}") if fixed else tr("bez poprawki", "no fix")
        lines.append(f"- {ids}" + (f" [{severity}]" if severity else "") + f" · {state}" + (f" · {summary}" if summary else ""))
    if len(rows) > MAX_VULNS:
        lines.append(tr(f"(i {len(rows) - MAX_VULNS} kolejnych)", f"(and {len(rows) - MAX_VULNS} more)"))
    return "\n".join(lines)
