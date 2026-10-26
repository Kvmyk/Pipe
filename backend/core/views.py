"""
Widok Pipe: `simple` (dla osob nietechnicznych) albo `tech` (serwer, schemat, podatnosci, zdalne cele).

Jeden wybor dla calego agenta, jak jezyk (komenda `view`: /widok w CLI, pipe web i na Telegramie). Zapisany
w DATA_DIR/view.json; dopoki nikt nie wybral, obowiazuje `tech` (tak dzialal Pipe przed widokami), a klienci
przy pierwszym polaczeniu pytaja, co uzytkownik chce robic. `guess()` zgaduje widok z opisu zwyklymi slowami
(bez modelu — dziala, zanim ktokolwiek doda klucz API).

Widok zmienia to, co klienci pokazuja (komendy w podpowiedziach, zakladki, menu Telegrama), i sposob mowienia
agenta (SIMPLE_VIEW_BLOCK w prompcie). Komendy technicznego widoku nadal dzialaja po wpisaniu.

Wylacznie biblioteka standardowa, bez importu settings.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

VIEWS = ("simple", "tech")
DEFAULT = "tech"
STORE = "view.json"
_NAMES = {
    "simple": "simple", "prosty": "simple", "prosta": "simple", "proste": "simple", "zwykly": "simple",
    "codzienny": "simple", "basic": "simple", "easy": "simple", "plain": "simple", "1": "simple",
    "tech": "tech", "techniczny": "tech", "techniczna": "tech", "technical": "tech", "zaawansowany": "tech",
    "advanced": "tech", "admin": "tech", "pro": "tech", "2": "tech",
}

# Slowa (bez polskich znakow, poczatki wyrazow) w opisie "co chcesz robic" — wiecej trafien wygrywa.
TECH_WORDS = (
    "serwer", "server", "vps", "docker", "kontener", "container", "kubernetes", "k8s", "nginx", "apache", "caddy",
    "traefik", "deploy", "wdroz", "wdraz", "ssh", "devops", "sysadmin", "admin", "infrastruk", "infrastruc",
    "monitor", "logi", "logs", "port", "dns", "certyfik", "certific", "ssl", "tls", "backup", "kopi", "baza danych",
    "database", "postgres", "mysql", "redis", "linux", "ubuntu", "debian", "systemd", "cron", "git", "repo",
    "ci/cd", "pipeline", "podatnos", "vulnerab", "latk", "patch", "aktualizacj", "firewall", "siec", "network",
    "programi", "programm", "kod", "code", "api", "aplikacj", "app", "strona", "site", "sklep", "hosting", "skrypt",
    "script", "terminal", "komend", "command",
)
SIMPLE_WORDS = (
    "mail", "poczt", "email", "kalendarz", "calendar", "spotkan", "meeting", "dokument", "document", "notatk",
    "note", "przypomn", "remind", "zakup", "shopping", "list", "tekst", "text", "napisz", "write", "pisa",
    "tlumacz", "translat", "streszcz", "summar", "plan", "organiz", "codzien", "everyday", "daily", "asystent",
    "assistant", "pomysl", "idea", "nauk", "learn",
    "przepis", "recipe", "podroz", "travel", "rachun", "invoice", "faktur", "excel", "arkusz", "spreadsheet",
    "prezentacj", "presentation", "zdjec", "photo", "social", "nie znam", "nie jestem", "laik",
    "nietechn", "non-techn", "not techn", "zwykl", "proste", "simple",
)


def _store_path() -> Path:
    # Jak memory.data_dir() — bez importu memory (ten modul jest tylko na bibliotece standardowej).
    custom = os.getenv("DATA_DIR", "").strip()
    return (Path(custom) if custom else Path(__file__).resolve().parent.parent / "data") / STORE


def _plain(text: str) -> str:
    """Male litery bez polskich znakow: 'Wdrożę' -> 'wdroze'."""
    text = unicodedata.normalize("NFKD", str(text or "").lower().replace("ł", "l"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize(value: str) -> str | None:
    """Nazwa widoku w dowolnym jezyku -> 'simple' / 'tech'; nieznana -> None."""
    return _NAMES.get(_plain(value).strip())


def guess(text: str) -> str | None:
    """Widok z opisu zwyklymi slowami ("chce ogarniac maile" -> simple); remis albo brak trafien -> None."""
    words = re.findall(r"[a-z0-9/+-]+", _plain(text))
    joined = " " + " ".join(words) + " "

    def hits(stems: tuple[str, ...]) -> int:
        return sum(1 for stem in stems if (" " + stem) in joined)

    tech, simple = hits(TECH_WORDS), hits(SIMPLE_WORDS)
    if tech == simple:
        return None
    return "tech" if tech > simple else "simple"


def _load() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def chosen() -> bool:
    """Czy ktos juz wybral widok — jesli nie, klienci pytaja przy pierwszym polaczeniu."""
    return _load().get("view") in VIEWS


def current() -> str:
    view = _load().get("view")
    return view if view in VIEWS else DEFAULT


def is_simple() -> bool:
    return current() == "simple"


def set_view(value: str) -> str:
    """Ustawia widok (nazwa albo opis zwyklymi slowami). Nieznany -> ValueError, blad zapisu -> OSError."""
    target = normalize(value) or guess(value)
    if target is None:
        raise ValueError(value)
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"view": target}), encoding="utf-8")
    os.replace(tmp, path)
    return target
