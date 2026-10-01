"""
Zdarzenia strumieniowane z agenta do klienta.

Handler i petla agenta yielduja:
  str          tekst dla uzytkownika (komunikaty protokolu, odpowiedz modelu)
  Attachment   plik dla uzytkownika — np. diagram PNG; Telegram wysyla go jako
               zdjecie, CLI zapisuje na dysk i pokazuje wersje tekstowa
  Progress     krotki status posredni (np. co robi worker); klienci pokazuja
               go na biezaco i nie wliczaja do odpowiedzi
  Activity     co agent wlasnie robi i na ktorym elemencie schematu — tylko dla
               klientow, ktore rysuja schemat na zywo (interfejs webowy)

server.py zamienia je na ramki protokolu (docs/protocol.md). Starsi klienci
ignoruja pola `attachment` i `event`, bo `response` jest wtedy pusty.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Attachment:
    name: str
    mime: str
    data: bytes
    caption: str = ""
    source: str = ""   # zrodlo, z ktorego powstal plik (np. kod Mermaid)
    text: str = ""     # wersja tekstowa dla terminala (np. diagram ASCII)

    def to_wire(self) -> dict:
        return {
            "name": self.name,
            "mime": self.mime,
            "caption": self.caption,
            "source": self.source,
            "text": self.text,
            "data": base64.b64encode(self.data).decode("ascii"),
        }


@dataclass(frozen=True)
class Progress:
    text: str
    source: str = ""   # np. "worker:web1"

    def to_wire(self) -> dict:
        return {"type": "progress", "text": self.text, "source": self.source}


@dataclass(frozen=True)
class Activity:
    """
    Dzialanie agenta przypisane do wezlow schematu (core/graph.py).
    phase: start | wait (czeka na potwierdzenie) | end. `entry` — wpis dziennika zmian (po wykonaniu zmiany).
    """
    id: str
    phase: str
    tool: str
    label: str
    nodes: tuple[str, ...] = ("host",)
    ok: bool | None = None
    entry: str = ""
    workers: tuple[tuple[str, str], ...] = ()    # (nazwa workera, cel) — dla delegate

    def to_wire(self) -> dict:
        return {"type": "activity", "id": self.id, "phase": self.phase, "tool": self.tool, "label": self.label,
                "nodes": list(self.nodes), "ok": self.ok, "entry": self.entry,
                "workers": {name: target for name, target in self.workers}}


Event = Union[str, Attachment, Progress, Activity]
