"""
Tools — definicje narzędzi dla LLM w formacie OpenAI function calling.
"""

from __future__ import annotations

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": (
                "Wykonuje komendę shell lokalnie na serwerze. "
                "Używaj tylko bezpiecznych komend zgodnych z allowlistą. "
                "Ustaw requires_confirmation=true dla operacji modyfikujących system "
                "(edycja plików konfiguracyjnych, zmiana uprawnień, restart usług, itp.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Komenda shell do wykonania na serwerze.",
                    },
                    "requires_confirmation": {
                        "type": "boolean",
                        "description": (
                            "Czy agent uważa tę operację za ryzykowną i wymaga "
                            "potwierdzenia użytkownika przed wykonaniem. "
                            "Ustaw true dla operacji modyfikujących system."
                        ),
                    },
                },
                "required": ["command", "requires_confirmation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Odczytuje zawartość pliku na serwerze. "
                "Używaj do przeglądania plików konfiguracyjnych, logów itp. "
                "Nie odczytuj plików zawierających sekrety (klucze prywatne, hasła) "
                "jeśli nie jest to absolutnie konieczne."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolutna ścieżka do pliku na serwerze.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Zapisuje lub edytuje plik na serwerze. "
                "ZAWSZE wymaga potwierdzenia użytkownika — ustaw requires_confirmation=true. "
                "NIE loguj zawartości pliku w rozmowie jeśli może zawierać sekrety."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolutna ścieżka do pliku na serwerze.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Nowa zawartość pliku.",
                    },
                    "requires_confirmation": {
                        "type": "boolean",
                        "description": "Zawsze true — zapis pliku wymaga potwierdzenia.",
                    },
                },
                "required": ["path", "content", "requires_confirmation"],
            },
        },
    },
]
