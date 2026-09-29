"""
Jezyk Pipe: PIPE_LANG=pl (domyslnie) albo en.

Tlumaczenia leza obok tekstu zrodlowego: `tr("polski", "english")` — przy zmianie
komunikatu od razu widac, ze trzeba poprawic oba. Prompty i schematy narzedzi maja
osobne wersje (config/prompts_en.py, core/tools_en.py), wybierane przez `prompt()`.

Znaczniki protokolu ([POTWIERDZ], [BLAD], [ODMOWA], [PAMIEC]) sa niezalezne od jezyka —
klienci zamieniaja je na etykiety w swoim jezyku.

Modul czyta zmienna srodowiskowa przy kazdym wywolaniu (jak core/runtime.py),
wiec testy moga przelaczac jezyk przez monkeypatch.setenv.
"""

from __future__ import annotations

import os


def lang() -> str:
    value = os.getenv("PIPE_LANG", "pl").strip().lower()
    return "en" if value.startswith("en") else "pl"


def is_en() -> bool:
    return lang() == "en"


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
