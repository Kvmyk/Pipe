"""
Obiekty danych dla agenta — sesja i żądania potwierdzenia.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal


@dataclass
class ConfirmationRequest:
    """Zdefiniowany w locie, gdy agent potrzebuje potwierdzenia od użytkownika."""

    tool_call_id: str
    tool_name: str
    command: str
    classification: Literal["confirm"]
    # Dla write_file — przechowuje ścieżkę (lokalną, widzianą przez proces Pipe) i zawartość
    file_path: str | None = None
    file_content: str | None = None
    # Operacja, ktora nie jest komenda shell (np. dodanie celu albo rutyny) —
    # wykonywana po TAK zamiast `command`, ktore wtedy jest tylko opisem.
    action: Callable[[], Awaitable[str]] | None = None
    # Plan bezpiecznika (core/safety.py) pokazany w potwierdzeniu: kopie, sprawdzenia,
    # weryfikacja. Wykonywany jest dokladnie ten plan, ktory widzial uzytkownik.
    plan: Any = None
    # Dzialanie na schemacie (events.Activity w fazie "wait") — po TAK interfejs webowy dostaje jego ciag dalszy.
    activity: Any = None


@dataclass
class Session:
    """Sesja użytkownika z historią wiadomości."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    interface: str = "cli"
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: ConfirmationRequest | None = None
    cwd: str = "/"
    # Historie rozmow workerow (nazwa -> wiadomosci) — agent moze wrocic do workera
    workers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Sesje techniczne (rutyny, workery) nie ucza sie stylu uzytkownika
    learns_vibe: bool = True
    # Kto zalozyl sesje (tozsamosc z tokenu) i z jaka rola: admin | viewer (tylko odczyty)
    owner: str = ""
    role: str = "admin"

    @property
    def is_telegram(self) -> bool:
        return self.interface.lower().startswith("telegram")

    @property
    def shows_activity(self) -> bool:
        """Interfejs webowy rysuje schemat na zywo — tylko on dostaje zdarzenia Activity."""
        return self.interface.lower().startswith("web")

    @property
    def user_key(self) -> str:
        """Klucz uzytkownika dla VIBE: `cli`, `telegram-123`."""
        from backend.core.memory import vibe_key
        return vibe_key(self.interface)

    @property
    def system_prompt(self) -> str:
        """
        System prompt dla LLM. Kolejnosc od najbardziej stalego do najbardziej
        zmiennego — providerzy cache'uja najdluzszy niezmieniony prefiks:
        baza -> tryb dzialania -> pamiec (SERVER.md, DIRECTORY, skille, VIBE)
        -> alerty czuwania -> katalog roboczy (zmienia sie najczesciej).
        """
        from backend.core.i18n import prompt, tr
        from backend.core import runtime
        from backend.core.memory import prompt_context
        from backend.core.watch import prompt_alerts

        base = prompt("TELEGRAM_SYSTEM_PROMPT" if self.is_telegram else "BASE_SYSTEM_PROMPT")
        return (
            base
            + tr("\n\n--- SRODOWISKO ---\n", "\n\n--- ENVIRONMENT ---\n") + runtime.describe()
            + prompt_context(self.user_key)
            + prompt_alerts()
            + (prompt("VIEWER_BLOCK") if self.role == "viewer" else "")
            + prompt("cwd_block")(self.cwd, telegram=self.is_telegram)
        )
