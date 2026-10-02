"""
Jezyk Pipe: PIPE_LANG=pl (domyslnie) albo en.

Tlumaczenia leza obok tekstu zrodlowego: `tr("polski", "english")` — przy zmianie
komunikatu od razu widac, ze trzeba poprawic oba. Prompty i schematy narzedzi maja
osobne wersje (config/prompts_en.py, core/tools_en.py), wybierane przez `prompt()`.

Znaczniki protokolu ([POTWIERDZ], [BLAD], [ODMOWA], [PAMIEC]) sa niezalezne od jezyka —
klienci zamieniaja je na etykiety w swoim jezyku.

Modul czyta zmienna srodowiskowa przy kazdym wywolaniu (jak core/runtime.py),
wiec testy moga przelaczac jezyk przez monkeypatch.setenv.

Jezyk mozna tez zmienic w trakcie pracy (komenda `language`: /jezyk w CLI, pipe web i na
Telegramie). Wybor trafia do DATA_DIR/language.json razem z jezykiem z .env z chwili wyboru —
po recznej zmianie PIPE_LANG w .env znow wygrywa .env. Wybor jest wspolny dla calego agenta.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

LANGUAGES = ("pl", "en")
STORE = "language.json"
_NAMES = {
    "pl": "pl", "pol": "pl", "polski": "pl", "polish": "pl", "po polsku": "pl",
    "en": "en", "eng": "en", "ang": "en", "angielski": "en", "english": "en", "po angielsku": "en",
}
_chosen = False        # czy obowiazuje wybor z language.json (a nie sam .env)


def lang() -> str:
    value = os.getenv("PIPE_LANG", "pl").strip().lower()
    return "en" if value.startswith("en") else "pl"


def is_en() -> bool:
    return lang() == "en"


def normalize(value: str) -> str | None:
    """'EN', 'english', 'angielski' -> 'en'; nieznany jezyk -> None."""
    return _NAMES.get(str(value).strip().lower())


def _store_path() -> Path:
    # Jak memory.data_dir() — bez importu memory (ten modul jest tylko na bibliotece standardowej).
    custom = os.getenv("DATA_DIR", "").strip()
    return (Path(custom) if custom else Path(__file__).resolve().parent.parent / "data") / STORE


def chosen() -> bool:
    """Czy jezyk zostal wybrany w trakcie pracy — wtedy klienci przejmuja go przy polaczeniu."""
    return _chosen


def load_saved() -> str:
    """Start serwera: przywraca jezyk wybrany w trakcie pracy, o ile .env nie zmienil sie od tamtej pory."""
    global _chosen
    _chosen = False
    try:
        saved = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return lang()
    if isinstance(saved, dict) and saved.get("lang") in LANGUAGES and saved.get("env") == lang():
        os.environ["PIPE_LANG"] = saved["lang"]
        _chosen = True
    return lang()


def set_lang(value: str) -> str:
    """Przelacza jezyk Pipe i zapisuje wybor. Nieznany jezyk -> ValueError, blad zapisu -> OSError (bez zmiany)."""
    global _chosen
    target = normalize(value)
    if target is None:
        raise ValueError(value)
    path = _store_path()
    try:
        base = json.loads(path.read_text(encoding="utf-8")).get("env") if _chosen else None
    except (OSError, json.JSONDecodeError, AttributeError):
        base = None
    if base not in LANGUAGES:
        base = lang()          # jezyk z .env — po kolejnych przelaczeniach zostaje ten sam
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"lang": target, "env": base}) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    os.environ["PIPE_LANG"] = target
    _chosen = True
    return target


def load_env_lang(env_file) -> None:
    """
    Dla narzedzi uruchamianych poza serwerem (kreator, tokeny): bierze PIPE_LANG
    z pliku .env, jesli nie ma go w srodowisku. Bez zaleznosci (python-dotenv).
    """
    if os.getenv("PIPE_LANG"):
        return
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.strip().partition("=")
                if sep and key.strip() == "PIPE_LANG" and value.strip():
                    os.environ["PIPE_LANG"] = value.strip().strip("'\"")
                    return
    except OSError:
        return


def tr(pl: str, en: str) -> str:
    """Tekst w jezyku Pipe."""
    return en if is_en() else pl


def prompt(name: str):
    """Stala z config/prompts.py w jezyku Pipe (angielska wersja z prompts_en.py, jesli istnieje)."""
    from backend.config import prompts

    if is_en():
        from backend.config import prompts_en
        if hasattr(prompts_en, name):
            return getattr(prompts_en, name)
    return getattr(prompts, name)


CONFIRM_PHRASES = ("wymaga potwierdzenia", "requires confirmation")
