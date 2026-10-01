"""
Sprawdzenia bez konfiguracji — Pipe zna serwer, wiec wie, co sprawdzac.

Zrodla sa odkrywane automatycznie, uzytkownik niczego nie definiuje:
  - domeny z konfiguracji reverse proxy (nginx, Caddy) i etykiet kontenerow
    (Traefik, VIRTUAL_HOST) -> waznosc certyfikatu TLS, odpowiedz HTTPS, rekord DNS
  - katalogi [backup] z DIRECTORY -> wiek najnowszego pliku

Czuwanie uruchamia je co CHECKS_INTERVAL, narzedzie server_history
(operation=checks) i komenda /zdrowie — na zadanie. Sprawdzenia sieciowe
mozna wylaczyc (WATCH_SITES=0), a pojedyncze domeny i sciezki pominac
(WATCH_IGNORE=a.example.com,/var/backups/stare).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.core import hostinfo, memory, runtime
from backend.core.i18n import tr

MAX_DOMAINS = 30
TLS_TIMEOUT = 8.0
HTTP_TIMEOUT = 10.0
BACKUP_WALK_DEPTH = 3
BACKUP_WALK_LIMIT = 20_000
USER_AGENT = "Pipe-healthcheck"


# ─── Domeny ─────────────────────────────────────────────────────────────────

def is_checkable_domain(name: str) -> bool:
    name = (name or "").strip().lower().rstrip(".")
    if not name or "." not in name or "*" in name or "$" in name or "~" in name or "/" in name:
        return False
    if name.endswith((".local", ".internal", ".lan", ".localhost", ".home", ".test", ".example", ".invalid")):
        return False
    try:
        ipaddress.ip_address(name.strip("[]"))
        return False
    except ValueError:
        pass
    return all(part and len(part) < 64 for part in name.split("."))


def domains_from(routes: list[Any], containers: list[Any] | None, ignore: set[str] = frozenset()) -> list[str]:
    found: set[str] = set()
    for route in routes:
        found.update(d.lower().rstrip(".") for d in getattr(route, "domains", []))
    for container in containers or []:
        found.update(d.lower().rstrip(".") for d in getattr(container, "domains", []))
    return sorted(d for d in found if is_checkable_domain(d) and d not in ignore)[:MAX_DOMAINS]


async def discover_domains(ignore: set[str] = frozenset()) -> list[str]:
    from backend.core import infra

    routes = await asyncio.to_thread(infra.proxy_routes)
    containers = await infra.docker_containers()
    return domains_from(routes, containers, ignore)


# ─── TLS, HTTPS, DNS ────────────────────────────────────────────────────────

@dataclass
class CertResult:
    domain: str
    days_left: float | None = None
    not_after: str = ""
    issuer: str = ""
    error: str = ""
    rejected: bool = False        # certyfikat odrzucony przy weryfikacji (nie: brak polaczenia)


@dataclass
class SiteResult:
    domain: str
    status: int | None = None
    millis: int | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.status is not None and self.status < 500


@dataclass
class DnsResult:
    domain: str
    addresses: list[str] = field(default_factory=list)
    error: str = ""
    points_here: bool | None = None   # None = nie da sie stwierdzic (brak publicznego IP na interfejsach)


async def check_cert(domain: str, port: int = 443, now: float | None = None) -> CertResult:
    result = CertResult(domain)
    context = ssl.create_default_context()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(domain, port, ssl=context, server_hostname=domain), TLS_TIMEOUT)
    except ssl.SSLCertVerificationError as exc:
        reason = exc.verify_message or exc.reason or exc
        result.error = tr(f"certyfikat odrzucony: {reason}", f"certificate rejected: {reason}")
        result.rejected = True
        return result
    except (OSError, asyncio.TimeoutError, ssl.SSLError) as exc:
        result.error = (tr("brak polaczenia TLS: ", "no TLS connection: ") + f"{type(exc).__name__}: {exc}").rstrip(": ")
        return result
    try:
        cert = writer.get_extra_info("peercert") or {}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    not_after = cert.get("notAfter")
    if not not_after:
        result.error = tr("serwer nie pokazal certyfikatu", "the server did not present a certificate")
        return result
    expires = ssl.cert_time_to_seconds(not_after)
    result.days_left = round((expires - (now if now is not None else time.time())) / 86400, 1)
    result.not_after = datetime.fromtimestamp(expires).strftime("%Y-%m-%d")
    issuer = dict(item[0] for item in cert.get("issuer", ()) if item)
    result.issuer = issuer.get("organizationName") or issuer.get("commonName") or ""
    return result


def _http_status(url: str) -> tuple[int | None, str]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 — domeny z konfiguracji hosta
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            # Certyfikat zglasza check_cert — tu liczy sie, czy strona w ogole odpowiada.
            return _http_status_insecure(url)
        return None, str(reason)
    except (OSError, ValueError) as exc:
        return None, str(exc)


def _http_status_insecure(url: str) -> tuple[int | None, str]:
    import urllib.error
    import urllib.request

    context = ssl._create_unverified_context()  # noqa: S323 — tylko kod odpowiedzi, bez tresci
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT, context=context) as response:  # noqa: S310
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(getattr(exc, "reason", exc))


async def check_site(domain: str) -> SiteResult:
    started = time.monotonic()
    status, error = await asyncio.to_thread(_http_status, f"https://{domain}/")
    return SiteResult(domain, status, round((time.monotonic() - started) * 1000), error)


def host_addresses(proc: str | None = None) -> set[str]:
    """Adresy IP interfejsow hosta z /proc/1/net (namespace hosta), bez petli zwrotnej."""
    base = proc or runtime.host_proc()
    addresses: set[str] = set()
    trie = hostinfo._read(f"{base}/1/net/fib_trie") or hostinfo._read(f"{base}/net/fib_trie")
    lines = trie.splitlines()
    for index, line in enumerate(lines):
        if "/32 host LOCAL" in line and index:
            candidate = lines[index - 1].strip().lstrip("|-+ ").strip()
            try:
                if not ipaddress.ip_address(candidate).is_loopback:
                    addresses.add(candidate)
            except ValueError:
                continue
    inet6 = hostinfo._read(f"{base}/1/net/if_inet6") or hostinfo._read(f"{base}/net/if_inet6")
    for line in inet6.splitlines():
        raw = line.split()[0] if line.split() else ""
        if len(raw) == 32:
            address = ipaddress.IPv6Address(bytes.fromhex(raw))
            if not (address.is_loopback or address.is_link_local):
                addresses.add(address.compressed)
    return addresses


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


async def check_dns(domain: str, local: set[str]) -> DnsResult:
    result = DnsResult(domain)
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(domain, None, type=socket.SOCK_STREAM), TLS_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:
        result.error = tr(f"nie rozwiazuje sie ({exc})", f"does not resolve ({exc})")
        return result
    result.addresses = sorted({info[4][0] for info in infos})
    public_local = {a for a in local if _is_public(a)}
    if public_local:
        result.points_here = bool(set(result.addresses) & public_local)
    return result


# ─── Backupy ────────────────────────────────────────────────────────────────

@dataclass
class BackupResult:
    path: str
    newest: float | None = None
    newest_name: str = ""
    files: int = 0
    error: str = ""

    def age_hours(self, now: float | None = None) -> float | None:
        if self.newest is None:
            return None
        return ((now if now is not None else time.time()) - self.newest) / 3600


def check_backup(host_path: str) -> BackupResult:
    result = BackupResult(host_path)
    local = runtime.to_local(host_path)
    if os.path.isfile(local):
        stat = os.stat(local)
        result.newest, result.newest_name, result.files = stat.st_mtime, os.path.basename(local), 1
        return result
    if not os.path.isdir(local):
        result.error = tr("sciezka nie istnieje", "path does not exist")
        return result
    base_depth = local.rstrip("/").count("/")
    seen = 0
    for current, dirs, files in os.walk(local):
        if current.count("/") - base_depth >= BACKUP_WALK_DEPTH:
            dirs[:] = []
        for name in files:
            seen += 1
            try:
                mtime = os.stat(os.path.join(current, name)).st_mtime
            except OSError:
                continue
            result.files += 1
            if result.newest is None or mtime > result.newest:
                result.newest, result.newest_name = mtime, os.path.relpath(os.path.join(current, name), local)
        if seen >= BACKUP_WALK_LIMIT:
            break
    if not result.files:
        result.error = tr("katalog jest pusty", "the directory is empty")
    return result


def backup_paths(ignore: set[str] = frozenset()) -> list[str]:
    try:
        return [e.path for e in memory.load_directory() if e.kind == "backup" and e.path not in ignore]
    except OSError:
        return []


# ─── Raport ─────────────────────────────────────────────────────────────────

@dataclass
class Report:
    certs: list[CertResult] = field(default_factory=list)
    sites: list[SiteResult] = field(default_factory=list)
    dns: list[DnsResult] = field(default_factory=list)
    backups: list[BackupResult] = field(default_factory=list)
    sites_enabled: bool = True
    at: float = field(default_factory=time.time)

    def findings(self, *, cert_days: int, backup_hours: int) -> list[Any]:
        """Znaleziska dla czuwania (klucze cert:, site:, dns:, backup:)."""
        from backend.core.watch import Finding

        out = []
        for cert in self.certs:
            if cert.rejected:
                out.append(Finding(f"cert:{cert.domain}", "critical",
                                   tr(f"Certyfikat {cert.domain} jest nieprawidlowy", f"The certificate for {cert.domain} is invalid"),
                                   cert.error))
            elif cert.days_left is not None and cert.days_left <= cert_days:
                severity = "critical" if cert.days_left <= 3 else "warning"
                days = max(0, int(cert.days_left))
                when = tr("wygasl", "has expired") if cert.days_left < 0 else tr(f"wygasa za {days} dni", f"expires in {days} days")
                out.append(Finding(f"cert:{cert.domain}", severity, tr(f"Certyfikat {cert.domain} {when}",
                                                                       f"The certificate for {cert.domain} {when}"),
                                   tr(f"wazny do {cert.not_after}", f"valid until {cert.not_after}")
                                   + (tr(f", wystawca {cert.issuer}", f", issuer {cert.issuer}") if cert.issuer else "")))
        for site in self.sites:
            if not site.ok:
                what = f"HTTP {site.status}" if site.status else site.error or tr("brak odpowiedzi", "no response")
                out.append(Finding(f"site:{site.domain}", "warning", tr(f"Strona {site.domain} nie dziala poprawnie",
                                                                        f"The site {site.domain} is not working correctly"),
                                   f"https://{site.domain}/ -> {what}"))
        for entry in self.dns:
            if entry.error:
                out.append(Finding(f"dns:{entry.domain}", "warning", tr(f"Domena {entry.domain} nie ma rekordu DNS",
                                                                         f"The domain {entry.domain} has no DNS record"),
                                   entry.error))
        for backup in self.backups:
            age = backup.age_hours(self.at)
            if backup.error:
                out.append(Finding(f"backup:{backup.path}", "warning", f"Backup {backup.path}: {backup.error}",
                                   tr("wpis [backup] w DIRECTORY", "[backup] entry in DIRECTORY")))
            elif age is not None and age > backup_hours:
                out.append(Finding(f"backup:{backup.path}", "warning",
                                   tr(f"Backup {backup.path} jest nieaktualny ({_age(age)})",
                                      f"The backup {backup.path} is stale ({_age(age)})"),
                                   tr(f"najnowszy plik: {backup.newest_name}", f"newest file: {backup.newest_name}")))
        return out

    def render(self, *, cert_days: int, backup_hours: int) -> str:
        lines: list[str] = []
        if not self.sites_enabled:
            lines.append(tr("Sprawdzenia sieciowe sa wylaczone (WATCH_SITES=0).", "Network checks are disabled (WATCH_SITES=0)."))
        elif not (self.certs or self.sites or self.dns):
            lines.append(tr("Nie znalazlem domen w konfiguracji reverse proxy (nginx, Caddy, Traefik) — "
                            "nie ma czego sprawdzac przez HTTPS.",
                            "I found no domains in the reverse proxy configuration (nginx, Caddy, Traefik) — "
                            "nothing to check over HTTPS."))
        if self.certs:
            lines.append(tr("Certyfikaty TLS:", "TLS certificates:"))
            for cert in self.certs:
                if cert.error:
                    lines.append(f"  - {cert.domain}: {cert.error}")
                else:
                    mark = " (!)" if cert.days_left is not None and cert.days_left <= cert_days else ""
                    issuer = f", {cert.issuer}" if cert.issuer else ""
                    days = int(cert.days_left or 0)
                    lines.append(tr(f"  - {cert.domain}: wazny jeszcze {days} dni (do {cert.not_after}){issuer}{mark}",
                                    f"  - {cert.domain}: valid for {days} more days (until {cert.not_after}){issuer}{mark}"))
        if self.sites:
            lines.append(tr("Odpowiedz HTTPS:", "HTTPS responses:"))
            for site in self.sites:
                what = tr(f"HTTP {site.status} w {site.millis} ms", f"HTTP {site.status} in {site.millis} ms") \
                    if site.status else tr(f"blad: {site.error}", f"error: {site.error}")
                lines.append(f"  - {site.domain}: {what}{'' if site.ok else ' (!)'}")
        if self.dns:
            lines.append("DNS:")
            for entry in self.dns:
                if entry.error:
                    lines.append(f"  - {entry.domain}: {entry.error} (!)")
                    continue
                where = {True: tr("ten serwer", "this server"),
                         False: tr("INNY adres niz ten serwer (CDN/proxy albo stary rekord?)",
                                   "a DIFFERENT address than this server (CDN/proxy or a stale record?)"),
                         None: tr("nie znam publicznego IP serwera — pomijam porownanie",
                                  "the server's public IP is unknown — skipping the comparison")}[entry.points_here]
                lines.append(f"  - {entry.domain} -> {', '.join(entry.addresses[:4])} ({where})")
        if self.backups:
            lines.append(tr("Backupy (wpisy [backup] w DIRECTORY):", "Backups ([backup] entries in DIRECTORY):"))
            for backup in self.backups:
                if backup.error:
                    lines.append(f"  - {backup.path}: {backup.error} (!)")
                    continue
                age = backup.age_hours(self.at) or 0
                mark = " (!)" if age > backup_hours else ""
                lines.append(tr(f"  - {backup.path}: najnowszy plik {_age(age)} temu ({backup.newest_name}), "
                                f"plikow: {backup.files}{mark}",
                                f"  - {backup.path}: newest file {_age(age)} ago ({backup.newest_name}), "
                                f"files: {backup.files}{mark}"))
        else:
            lines.append(tr("Backupy: brak wpisow [backup] w DIRECTORY — dopisz katalog backupow "
                            "(directory upsert kind=backup), a bede pilnowal jego swiezosci.",
                            "Backups: no [backup] entries in DIRECTORY — add the backup directory "
                            "(directory upsert kind=backup) and I will watch its freshness."))
        return "\n".join(lines)


def _age(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} min"
    if hours < 48:
        return f"{int(hours)} h"
    return tr(f"{int(hours / 24)} dni", f"{int(hours / 24)} days")


def parse_ignore(raw: str) -> set[str]:
    return {item.strip().lower().rstrip(".") if not item.strip().startswith("/") else item.strip()
            for item in (raw or "").split(",") if item.strip()}


async def run_checks(*, sites: bool = True, ignore: set[str] = frozenset(), proc: str | None = None) -> Report:
    """Wszystkie sprawdzenia naraz (rownolegle dla domen)."""
    report = Report(sites_enabled=sites)
    report.backups = [await asyncio.to_thread(check_backup, p) for p in backup_paths(ignore)]
    if not sites:
        return report
    domains = await discover_domains(ignore)
    if not domains:
        return report
    local = await asyncio.to_thread(host_addresses, proc)
    report.dns = list(await asyncio.gather(*(check_dns(d, local) for d in domains)))
    resolvable = [entry.domain for entry in report.dns if not entry.error]
    report.certs = list(await asyncio.gather(*(check_cert(d) for d in resolvable)))
    report.sites = list(await asyncio.gather(*(check_site(d) for d in resolvable)))
    return report
