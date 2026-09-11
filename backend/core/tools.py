"""
Tools -- definicje narzedzi dla LLM w formacie OpenAI function calling.

PipeClaw v0.5.0
"""

from __future__ import annotations

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": (
                "Wykonuje komende shell lokalnie na serwerze. "
                "Uzywaj tylko bezpiecznych komend zgodnych z allowlista. "
                "Ustaw requires_confirmation=true dla operacji modyfikujacych system "
                "(edycja plikow konfiguracyjnych, zmiana uprawnien, restart uslug, itp.)."
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
                            "Czy agent uwaza te operacje za ryzykowna i wymaga "
                            "potwierdzenia uzytkownika przed wykonaniem. "
                            "Ustaw true dla operacji modyfikujacych system."
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
                "Odczytuje zawartosc pliku na serwerze. "
                "Uzywaj do przegladania plikow konfiguracyjnych, logow itp. "
                "Nie odczytuj plikow zawierajacych sekrety (klucze prywatne, hasla) "
                "jesli nie jest to absolutnie konieczne."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolutna sciezka do pliku na serwerze.",
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
                "ZAWSZE wymaga potwierdzenia uzytkownika -- ustaw requires_confirmation=true. "
                "NIE loguj zawartosci pliku w rozmowie jesli moze zawierac sekrety."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolutna sciezka do pliku na serwerze.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Nowa zawartosc pliku.",
                    },
                    "requires_confirmation": {
                        "type": "boolean",
                        "description": "Zawsze true -- zapis pliku wymaga potwierdzenia.",
                    },
                },
                "required": ["path", "content", "requires_confirmation"],
            },
        },
    },
    # --- Nawigacja po katalogach ---
    {
        "type": "function",
        "function": {
            "name": "change_directory",
            "description": (
                "Zmienia wirtualny katalog roboczy na serwerze "
                "(hostfs). Np. '/home/user'. Uzywaj tego aby pamietac "
                "w jakim katalogu na serwerze uzytkownik chce pracowac."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Sciezka do katalogu na serwerze, np. '/home/user/my-project'",
                    },
                },
                "required": ["path"],
            },
        },
    },
    # --- Git ---
    {
        "type": "function",
        "function": {
            "name": "git_command",
            "description": (
                "Wykonuje operacje Git w podanym repozytorium. "
                "Obsluguje: status, log, diff, branch, checkout, pull, add, commit, push, stash. "
                "Operacje modyfikujace (commit, push, checkout, merge, stash pop) wymagaja potwierdzenia. "
                "Operacje odczytujace (status, log, diff, branch --list) sa bezpieczne."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": (
                            "Absolutna sciezka do katalogu repozytorium Git. "
                            "Np. /hostfs/home/user/my-project"
                        ),
                    },
                    "subcommand": {
                        "type": "string",
                        "description": (
                            "Podkomenda Git do wykonania, np.: "
                            "status, log --oneline -20, diff, branch, "
                            "pull, add ., commit -m 'msg', push, stash, stash pop"
                        ),
                    },
                    "requires_confirmation": {
                        "type": "boolean",
                        "description": (
                            "Ustaw true dla operacji modyfikujacych repozytorium "
                            "(commit, push, checkout, merge, reset, stash pop). "
                            "False dla operacji tylko odczytujacych (status, log, diff, branch --list)."
                        ),
                    },
                },
                "required": ["repo_path", "subcommand", "requires_confirmation"],
            },
        },
    },
    # --- Szczegolowy status systemu ---
    {
        "type": "function",
        "function": {
            "name": "system_stats",
            "description": (
                "Pobiera szczegolowe statystyki systemowe serwera: "
                "zuzycie CPU (per rdzen), zuzycie RAM (dokladne wartosci z /proc/meminfo), "
                "obciazenie systemu (load average), zuzycie dysku, uptime, "
                "lista najciezszych procesow (top 10 po CPU/RAM). "
                "Wszystkie dane pobierane sa z /proc i narzedzi systemowych -- NIE z uproszczonego 'free'."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # --- Docker management ---
    {
        "type": "function",
        "function": {
            "name": "docker_manage",
            "description": (
                "Zarzadza kontenerami Docker na serwerze. "
                "Operacje: ps (lista), logs (logi kontenera), inspect (szczegoly), "
                "stats (zuzycie zasobow), top (procesy w kontenerze), "
                "restart/stop/start (zarzadzanie cyklem zycia), "
                "images (lista obrazow), prune (czyszczenie nieuzywanych zasobow). "
                "Operacje modyfikujace (restart, stop, start, prune, rm, rmi) wymagaja potwierdzenia."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "ps", "logs", "inspect", "stats", "top",
                            "restart", "stop", "start", "rm", "rmi",
                            "images", "prune", "compose-ps", "compose-logs",
                        ],
                        "description": "Operacja Docker do wykonania.",
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "Nazwa lub ID kontenera/obrazu (wymagane dla operacji na konkretnym kontenerze). "
                            "Opcjonalne dla ps, images, prune, stats."
                        ),
                    },
                    "options": {
                        "type": "string",
                        "description": (
                            "Dodatkowe flagi, np. '--tail 50' dla logs, "
                            "'--all' dla ps, '--format json' dla inspect."
                        ),
                    },
                    "requires_confirmation": {
                        "type": "boolean",
                        "description": (
                            "True dla operacji modyfikujacych (restart, stop, start, rm, rmi, prune). "
                            "False dla operacji odczytujacych (ps, logs, inspect, stats, top, images)."
                        ),
                    },
                },
                "required": ["operation", "requires_confirmation"],
            },
        },
    },
    # --- Siec ---
    {
        "type": "function",
        "function": {
            "name": "network_info",
            "description": (
                "Diagnostyka sieciowa serwera: otwarte porty, aktywne polaczenia, "
                "nasluchujace uslugi, testy polaczenia (ping, curl). "
                "Przydatne do debugowania problemow z siecia i sprawdzania dostepu do uslug."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "check_type": {
                        "type": "string",
                        "enum": ["ports", "connections", "listeners", "ping", "curl", "dns"],
                        "description": (
                            "Typ diagnozy: "
                            "ports -- otwarte porty (ss -tlnp), "
                            "connections -- aktywne polaczenia (ss -tnp), "
                            "listeners -- nasluchujace uslugi (ss -tlnp), "
                            "ping -- test dostepnosci hosta, "
                            "curl -- test HTTP endpointu, "
                            "dns -- rozwiazywanie nazw DNS."
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "Cel diagnozy -- wymagany dla ping, curl, dns. "
                            "Np. 'google.com', 'http://localhost:8080/health', '1.1.1.1'"
                        ),
                    },
                },
                "required": ["check_type"],
            },
        },
    },
    # --- Cron ---
    {
        "type": "function",
        "function": {
            "name": "cron_manage",
            "description": (
                "Zarzadzanie zadaniami cron na serwerze. "
                "Operacje: list (wyswietl crontab), add (dodaj zadanie), "
                "remove (usun zadanie), check-logs (sprawdz logi wykonan). "
                "Dodawanie i usuwanie zadan wymaga potwierdzenia."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["list", "add", "remove", "check-logs"],
                        "description": "Operacja cron do wykonania.",
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "Harmonogram cron (wymagany dla 'add'). "
                            "Np. '0 3 * * *' (codziennie o 3:00), '*/5 * * * *' (co 5 minut)."
                        ),
                    },
                    "command": {
                        "type": "string",
                        "description": "Komenda do zaplanowania (wymagana dla 'add') lub wzorzec do usuniecia (dla 'remove').",
                    },
                    "requires_confirmation": {
                        "type": "boolean",
                        "description": "True dla add/remove, false dla list/check-logs.",
                    },
                },
                "required": ["operation", "requires_confirmation"],
            },
        },
    },
]
