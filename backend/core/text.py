"""
Pomocnicze formatowanie tekstu w komunikatach protokolu.
"""

from __future__ import annotations

import re

# Znaki, ktore moga zafalszowac to, co uzytkownik widzi w potwierdzeniu:
# sekwencje sterujace terminala (C0/C1 poza \n, \t), przelaczniki kierunku pisma
# (bidi, U+202A–E, U+2066–9, RLM/LRM), znaki zerowej szerokosci i BOM.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]")


def visible(text: str) -> str:
    """Zamienia niewidoczne/sterujace znaki na zapis \\xNN — 'co widzisz, to wykonasz'."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def as_code(text: str) -> str:
    """
    Zapisuje tekst jako kod Markdown, ktory klienci pokaza znak w znak.

    Komendy do potwierdzenia musza byc widoczne dokladnie tak, jak zostana
    wykonane — rowniez gdy same zawieraja backticki (np. `whoami`) albo
    kilka linii (heredoc). Otoczka jest o jeden backtick dluzsza od
    najdluzszego ciagu backtickow w tekscie (regula CommonMark); tekst
    wieloliniowy trafia do bloku kodu. Znaki sterujace i bidi sa zamieniane
    na widoczny zapis, zeby nie ukryly roznicy miedzy pokazana a wykonana komenda.
    """
    text = visible(text)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)

    if "\n" in text:
        fence = "`" * max(3, longest + 1)
        return f"\n{fence}\n{text}\n{fence}\n"

    fence = "`" * (longest + 1)
    # CommonMark zdejmuje po jednej spacji z obu stron — potrzebne, gdy tekst
    # zaczyna sie lub konczy backtickiem, zeby nie zlal sie z otoczka.
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"
