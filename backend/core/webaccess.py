"""
Wejscie do `pipe web` wystawionego publicznie (`/web wlacz`, core/webpublic.py) — parowanie urzadzen przez rozmowe.

Strona pod publicznym adresem nie ma hasla ani klucza w linku. Nowe urzadzenie (telefon, komputer) prosi o dostep
i dostaje krotki kod, np. `K7P-4QX`. Administrator widzi w Telegramie / CLI / `pipe web` wiadomosc "logowanie
z iPhone (Safari), IP ..., kod K7P-4QX" z przyciskami *Wpusc* / *Tylko podglad* / *Odrzuc* (albo pisze
`/web wpusc K7P-4QX`). Po zgodzie urzadzenie dostaje wlasny token (backend/tokens.py, nazwa `web-...`, rola
admin albo viewer) w ciasteczku HttpOnly — w tokens.json lezy tylko jego skrot. Link, ktory wycieknie, nic nie
daje: bez zgody z rozmowy nikt nie wejdzie, a kod na ekranie pozwala porownac, czy zatwierdzasz swoje urzadzenie.

Urzadzenie nieuzywane przez DEVICE_IDLE_DAYS wygasa (`expires` przesuwany najwyzej raz na godzine); `/web odwolaj`
usuwa je od razu. Prosby o dostep zyja w pamieci procesu (PAIR_TTL), z limitem na adres IP.

Bez importu `settings`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from backend import tokens
from backend.core.i18n import tr

PAIR_TTL = 300                    # prosba o dostep czeka 5 minut
COLLECT_TTL = 120                 # zatwierdzony token czeka na odbior przez strone 2 minuty
MAX_PENDING = 10
IP_LIMIT = 5                      # prosb z jednego adresu ...
IP_WINDOW = 600                   # ... na 10 minut
DEVICE_IDLE_DAYS = 30
TOUCH_EVERY = 3600
PREFIX = "web-"
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # bez 0/O, 1/I/L — kod przepisuje sie z ekranu
_CODE = re.compile(r"[^A-Z0-9]")
_lock = threading.RLock()


class AccessError(ValueError):
    """Bledna prosba, kod albo urzadzenie — tekst idzie do uzytkownika."""


@dataclass
class Pairing:
    id: str
    code: str
    secret: str                   # skrot sekretu strony, ktora poprosila (tylko ona odbierze token)
    device: str                   # "iPhone · Safari"
    ip: str
    created: float = field(default_factory=time.time)
    status: str = "pending"       # pending | approved | denied | expired | collected
    role: str = ""
    decided_by: str = ""
    decided: float = 0.0
    token: str = ""               # do odbioru przez strone; po odbiorze kasowany
    name: str = ""                # nazwa tokenu urzadzenia

    def to_event(self) -> dict[str, Any]:
        return {"type": "login", "id": self.id, "code": self.code, "device": self.device, "ip": self.ip}


_pairings: dict[str, Pairing] = {}
_by_ip: dict[str, list[float]] = {}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(6))
    return f"{raw[:3]}-{raw[3:]}"


def normalize_code(value: str) -> str:
    raw = _CODE.sub("", (value or "").upper())
    return f"{raw[:3]}-{raw[3:]}" if len(raw) == 6 else ""


def device_label(user_agent: str) -> str:
    """Krotki opis urzadzenia z User-Agent (tylko do pokazania — nie jest dowodem niczego)."""
    ua = user_agent or ""
    system = next((name for pattern, name in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
        ("Mac OS X", "Mac"), ("CrOS", "ChromeOS"), ("Linux", "Linux")) if pattern in ua), "")
    browser = next((name for pattern, name in (
        ("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"), ("Firefox/", "Firefox"),
        ("FxiOS", "Firefox"), ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if pattern in ua), "")
    label = " · ".join(part for part in (system, browser) if part)
    return label or tr("nieznane urzadzenie", "unknown device")


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:20] or "device"


def _expire(now: float) -> None:
    for item in list(_pairings.values()):
        if item.status == "pending" and now - item.created > PAIR_TTL:
            item.status = "expired"
        if item.status == "approved" and now - item.decided > COLLECT_TTL:
            item.status, item.token = "expired", ""
            if item.name:
                tokens.revoke(item.name)          # strona nie odebrala tokenu — nie zostawiamy go w tokens.json
        if now - item.created > 3600:
            del _pairings[item.id]
    for ip in list(_by_ip):
        _by_ip[ip] = [t for t in _by_ip[ip] if now - t < IP_WINDOW]
        if not _by_ip[ip]:
            del _by_ip[ip]


def start(user_agent: str, ip: str, now: float | None = None) -> tuple[Pairing, str]:
    """Nowa prosba o dostep. Zwraca (prosba, sekret dla strony)."""
    now = now if now is not None else time.time()
    ip = (ip or "?")[:64]
    with _lock:
        _expire(now)
        if len(_by_ip.get(ip, [])) >= IP_LIMIT:
            raise AccessError(tr("Za duzo prob z tego adresu — sprobuj za kilka minut.",
                                 "Too many attempts from this address — try again in a few minutes."))
        if sum(1 for p in _pairings.values() if p.status == "pending") >= MAX_PENDING:
            raise AccessError(tr("Za duzo oczekujacych prosb o dostep — sprobuj za kilka minut.",
                                 "Too many pending access requests — try again in a few minutes."))
        codes = {p.code for p in _pairings.values()}
        code = new_code()
        while code in codes:
            code = new_code()
        secret = secrets.token_urlsafe(24)
        pairing = Pairing(secrets.token_hex(6), code, _digest(secret), device_label(user_agent), ip, created=now)
        _pairings[pairing.id] = pairing
        _by_ip.setdefault(ip, []).append(now)
    return pairing, secret


def find(ref: str, now: float | None = None) -> Pairing | None:
    """Prosba po kodzie (dowolny zapis: k7p4qx, K7P-4QX) albo po id."""
    with _lock:
        _expire(now if now is not None else time.time())
        if ref in _pairings:
            return _pairings[ref]
        code = normalize_code(ref)
        return next((p for p in _pairings.values() if code and p.code == code), None)


def pending(now: float | None = None) -> list[Pairing]:
    with _lock:
        _expire(now if now is not None else time.time())
        return sorted((p for p in _pairings.values() if p.status == "pending"), key=lambda p: p.created)


def decide(ref: str, approve: bool, *, role: str = "admin", by: str = "", now: float | None = None) -> Pairing:
    """Zgoda albo odmowa administratora. Zgoda tworzy token urzadzenia (czeka na odbior przez strone)."""
    now = now if now is not None else time.time()
    with _lock:
        pairing = find(ref, now)
        if pairing is None:
            raise AccessError(tr("Nie ma takiej prosby o dostep (kod wygasa po 5 minutach). Popros na stronie jeszcze raz.",
                                 "No such access request (codes expire after 5 minutes). Ask again on the page."))
        if pairing.status != "pending":
            state = {"approved": tr("juz wpuszczone", "already let in"), "collected": tr("juz wpuszczone", "already let in"),
                     "denied": tr("odrzucona", "rejected"), "expired": tr("wygasla", "expired")}.get(pairing.status, pairing.status)
            raise AccessError(tr(f"Prosba {pairing.code}: {state}.", f"Request {pairing.code}: {state}."))
        pairing.decided_by, pairing.decided = by, now
        if not approve:
            pairing.status = "denied"
            return pairing
        if role not in tokens.ROLES:
            raise AccessError(tr("Rola: admin albo viewer.", "Role: admin or viewer."))
        name = f"{PREFIX}{_slug(pairing.device)}-{secrets.token_hex(2)}"
        pairing.token = tokens.add(name, role, extra={
            "label": pairing.device, "ip": pairing.ip, "approved_by": by,
            "expires": int(now + DEVICE_IDLE_DAYS * 86400), "seen": int(now)})
        pairing.status, pairing.role, pairing.name = "approved", role, name
        return pairing


def collect(pairing_id: str, secret: str, now: float | None = None) -> dict[str, Any]:
    """Stan prosby dla strony, ktora ja wyslala; zatwierdzony token oddawany jest raz."""
    with _lock:
        pairing = _pairings.get(pairing_id or "")
        if pairing is None or not hmac.compare_digest(pairing.secret, _digest(secret or "")):
            return {"status": "unknown"}
        _expire(now if now is not None else time.time())
        if pairing.status == "approved" and pairing.token:
            token, pairing.token, pairing.status = pairing.token, "", "collected"
            return {"status": "approved", "token": token, "role": pairing.role}
        return {"status": pairing.status}


def device(token: str, now: float | None = None) -> tuple[str, str] | None:
    """(nazwa, rola) urzadzenia pipe web dla tokenu z ciasteczka; uzycie przesuwa wygasniecie."""
    found = tokens.match(token or "")
    if found is None or not found[0].startswith(PREFIX):
        return None
    now = now if now is not None else time.time()
    entry = next((e for e in tokens.load() if e.get("name") == found[0]), {})
    if now - float(entry.get("seen") or 0) > TOUCH_EVERY:
        tokens.update(found[0], seen=int(now), expires=int(now + DEVICE_IDLE_DAYS * 86400))
    return found


def devices() -> list[dict[str, Any]]:
    """Urzadzenia pipe web (bez skrotow tokenow), od najstarszego."""
    now = time.time()
    found = []
    for entry in tokens.load():
        if not str(entry.get("name", "")).startswith(PREFIX):
            continue
        expires = entry.get("expires")
        found.append({"name": entry["name"], "label": entry.get("label") or entry["name"], "role": entry["role"],
                      "created": entry.get("created", ""), "ip": entry.get("ip", ""),
                      "seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["seen"])) if entry.get("seen") else "",
                      "expired": isinstance(expires, (int, float)) and expires < now})
    return found


def revoke(ref: str) -> list[str]:
    """Odwoluje urzadzenie: numer z listy, nazwa albo `wszystkie` / `all`. Zwraca opisy odwolanych."""
    found = devices()
    ref = (ref or "").strip().lower()
    if ref in ("wszystkie", "all", "*"):
        chosen = found
    elif ref.isdigit() and 1 <= int(ref) <= len(found):
        chosen = [found[int(ref) - 1]]
    else:
        chosen = [d for d in found if d["name"] == ref]
    if not chosen:
        raise AccessError(tr("Nie ma takiego urzadzenia — numery i nazwy pokazuje /web.",
                             "No such device — /web shows the numbers and names."))
    for entry in chosen:
        tokens.revoke(entry["name"])
    return [f"{d['label']} ({d['name']})" for d in chosen]


def reset() -> None:
    """Testy: czysci prosby w pamieci."""
    with _lock:
        _pairings.clear()
        _by_ip.clear()
