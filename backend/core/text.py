"""
Pomocnicze formatowanie tekstu w komunikatach protokolu.
"""

from __future__ import annotations

import re


def as_code(text: str) -> str:
    """
    Zapisuje tekst jako kod Markdown, ktory klienci pokaza znak w znak.

    Komendy do potwierdzenia musza byc widoczne dokladnie tak, jak zostana
    wykonane — rowniez gdy same zawieraja backticki (np. `whoami`) albo
    kilka linii (heredoc). Otoczka jest o jeden backtick dluzsza od
    najdluzszego ciagu backtickow w tekscie (regula CommonMark); tekst
    wieloliniowy trafia do bloku kodu.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)

    if "\n" in text:
        fence = "`" * max(3, longest + 1)
        return f"\n{fence}\n{text}\n{fence}\n"

    fence = "`" * (longest + 1)
    # CommonMark zdejmuje po jednej spacji z obu stron — potrzebne, gdy tekst
    # zaczyna sie lub konczy backtickiem, zeby nie zlal sie z otoczka.
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"
