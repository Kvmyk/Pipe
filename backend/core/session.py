"""
Obiekty danych dla agenta — sesja i żądania potwierdzenia.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ConfirmationRequest:
    """Zdefiniowany w locie, gdy agent potrzebuje potwierdzenia od użytkownika."""

    tool_call_id: str
    tool_name: str
    command: str
    classification: Literal["confirm"]
    # Dla write_file — przechowuje ścieżkę i zawartość
    file_path: str | None = None
    file_content: str | None = None


@dataclass
class Session:
    """Sesja użytkownika z historią wiadomości."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    interface: str = "cli"
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: ConfirmationRequest | None = None
    cwd: str = "/"

    @property
    def system_prompt(self) -> str:
        """Generuje system prompt dla LLM w zależności od interfejsu."""
        from backend.config.prompts import BASE_SYSTEM_PROMPT, TELEGRAM_SYSTEM_PROMPT
        from backend.core.memory import prompt_context
        
        dir_context = (
            f"\n\n--- NAWIGACJA ---\n"
            f"Twoj wirtualny katalog roboczy na serwerze to obecnie: {self.cwd}\n"
            f"(Pamietaj, ze w kontenerze ten katalog znajduje sie pod sciezka /hostfs{self.cwd}).\n"
            f"1. Jesli uruchamiasz komendy plikowe lokalnie dla tego katalogu, uzyj sciezki /hostfs{self.cwd}.\n"
            f"2. ZA KAZDYM RAZEM gdy odpisujesz uzytkownikowi, ZAWSZE rozpoczynaj pierwsza linie od "
            f"tagu reprezentujacego aktualna sciezke, np.: [Katalog: {self.cwd}]\n"
            f"3. Uzyj narzedzia change_directory, jesli uzytkownik prosi o wejscie/przejscie do innego folderu.\n"
        )
        if "telegram" in self.interface.lower():
            # W telegramie system_prompt uzywa HTML
            dir_context = dir_context.replace("[Katalog: ", "<b>[Katalog: ")
            dir_context = dir_context.replace("]", "]</b>")
            return TELEGRAM_SYSTEM_PROMPT + dir_context + prompt_context()
        return BASE_SYSTEM_PROMPT + dir_context + prompt_context()

