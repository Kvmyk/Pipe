"""
Renderowanie diagramow Mermaid do PNG (dla Telegrama i plikow) i ASCII (dla terminala).

Uzywa mermaidx: prawdziwa biblioteka Mermaid uruchomiona w osadzonym QuickJS,
rasteryzacja przez resvg — bez przegladarki, Node.js i sieci. Kod diagramu
nigdy nie opuszcza serwera (w przeciwienstwie do uslug typu mermaid.ink).

Gdy mermaidx nie jest zainstalowany, render() zwraca sam kod — klient pokaze go
jako blok tekstu.
"""

from __future__ import annotations

import asyncio
import html
import re
import struct
import threading
from dataclasses import dataclass

MAX_MERMAID_CHARS = 30_000
# Telegram odrzuca zdjecia, ktorych szerokosc + wysokosc przekracza 10000 px.
MAX_PNG_SIDES = 9_000
BACKGROUND = "#ffffff"
MAX_ASCII_LINES = 120

_render_lock = threading.Lock()  # silnik JS nie jest wspoldzielony miedzy watkami


class DiagramError(ValueError):
    """Blad skladni albo renderowania — tresc wraca do modelu, zeby poprawil kod."""


@dataclass(frozen=True)
class Rendered:
    source: str
    png: bytes | None
    ascii: str


def available() -> bool:
    try:
        import mermaidx  # noqa: F401
    except ImportError:
        return False
    return True


def clean_source(source: str) -> str:
    """Zdejmuje otoczke ```mermaid``` i dyrektywy, ktore w obrazie nie maja sensu (click, linki)."""
    text = (source or "").strip()
    fence = re.match(r"^```(?:mermaid)?\s*\n(.*?)\n?```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    lines = [line for line in text.splitlines() if not re.match(r"^\s*click\s", line)]
    text = "\n".join(lines).strip()
    if not text:
        raise DiagramError("Pusty kod diagramu.")
    if len(text) > MAX_MERMAID_CHARS:
        raise DiagramError(f"Diagram za duzy ({len(text)} > {MAX_MERMAID_CHARS} znakow) — uprosc go.")
    return text


def ascii_source(source: str) -> str:
    """Wariant kodu dla renderera tekstowego: bez HTML w etykietach i bez frontmattera."""
    text = re.sub(r"^---\n.*?\n---\n", "", source, flags=re.DOTALL)
    text = text.replace("<br/>", " · ").replace("#quot;", "'")
    # renderer tekstowy nie rozumie `subgraph id["tytul"]` — pokazalby to doslownie
    text = re.sub(r'(?m)^(\s*subgraph\s+\w+)\["([^"\n]*)"\]', r"\1 [\2]", text)
    return html.unescape(text)


def png_size(data: bytes) -> tuple[int, int]:
    """Szerokosc i wysokosc z naglowka IHDR pliku PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return struct.unpack(">II", data[16:24])


def _render_sync(source: str, want_png: bool, want_ascii: bool) -> Rendered:
    import mermaidx

    png: bytes | None = None
    with _render_lock:
        try:
            diagram = mermaidx.render(source)
            if want_png:
                png = diagram.png(scale=2.0, background=BACKGROUND)
                width, height = png_size(png)
                if width + height > MAX_PNG_SIDES:
                    factor = max(0.5, 2.0 * MAX_PNG_SIDES / (width + height))
                    png = diagram.png(scale=factor, background=BACKGROUND)
        except Exception as exc:
            message = str(exc).replace("Mermaid rendering failed: ", "").strip()
            raise DiagramError(message[:1500] or type(exc).__name__) from exc
        text = ""
        if want_ascii:
            try:
                text = mermaidx.render_ascii(ascii_source(source))
            except Exception:
                text = ""  # ASCII to dodatek — nie kazdy typ diagramu go obsluguje
            if text.count("\n") > MAX_ASCII_LINES:
                text = ""  # za wysoki na terminal — zostaje PNG
    return Rendered(source, png, text)


# Limit czasu renderowania — zlosliwie zlozony diagram nie moze zawiesic watku ani trzymac blokady.
RENDER_TIMEOUT = 20.0


async def render(source: str, *, png: bool = True, ascii_art: bool = True) -> Rendered:
    """Renderuje kod Mermaid. Rzuca DiagramError przy bledzie skladni albo przekroczeniu czasu."""
    source = clean_source(source)
    if not available():
        return Rendered(source, None, "")
    try:
        return await asyncio.wait_for(asyncio.to_thread(_render_sync, source, png, ascii_art), RENDER_TIMEOUT)
    except asyncio.TimeoutError:
        # Watek renderujacy dokonczy w tle i zwolni blokade; my nie czekamy dluzej.
        raise DiagramError(f"Renderowanie przekroczylo {int(RENDER_TIMEOUT)} s — uprosc diagram.") from None
