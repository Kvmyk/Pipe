"""
Tools -- definicje narzedzi dla LLM w formacie OpenAI function calling.

Pipe v0.18.0

Kazde narzedzie ma handler `handle_<nazwa>` w backend/core/handlers/.
"""

from __future__ import annotations


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


_CONFIRM_FLAG = {
    "type": "boolean",
    "description": (
        "Opcjonalne. true wymusza pytanie o potwierdzenie nawet dla operacji, ktora klasyfikator uznalby "
        "za odczyt. Nie musisz go ustawiac: operacje zmieniajace stan i tak wymagaja potwierdzenia."
    ),
}

TOOLS: list[dict] = [
    _tool(
        "execute_command",
        "Wykonuje komende shell na serwerze w katalogu roboczym. Odczyty wykonuja sie od razu, komendy "
        "zmieniajace stan czekaja na potwierdzenie uzytkownika, zakazane sa odrzucane. Ogranicz output "
        "(tail -n, head, grep). Dla znanych obszarow wolisz specjalizowane narzedzia.",
        {
            "command": {"type": "string", "description": "Komenda shell do wykonania na serwerze."},
            "requires_confirmation": _CONFIRM_FLAG,
        },
        ["command"],
    ),
    _tool(
        "read_file",
        "Odczytuje plik na serwerze. Sciezka hosta (np. /etc/nginx/nginx.conf) albo wzgledna od katalogu "
        "roboczego. Sekrety w tresci sa ukrywane przed Toba ([ZREDAGOWANO]).",
        {"path": {"type": "string", "description": "Sciezka do pliku na serwerze."}},
        ["path"],
    ),
    _tool(
        "write_file",
        "Zapisuje caly plik na serwerze (tworzy albo nadpisuje). ZAWSZE wymaga potwierdzenia. Nie uzywaj dla "
        "plikow, ktore czytales z [ZREDAGOWANO] — zmien je punktowo przez execute_command (sed -i).",
        {
            "path": {"type": "string", "description": "Sciezka do pliku na serwerze."},
            "content": {"type": "string", "description": "Nowa, pelna zawartosc pliku."},
        },
        ["path", "content"],
    ),
    _tool(
        "change_directory",
        "Zmienia katalog roboczy na serwerze (np. '/srv/app'). Uzywaj, gdy uzytkownik chce pracowac w innym "
        "katalogu — kolejne komendy wykonuja sie w nim.",
        {"path": {"type": "string", "description": "Sciezka hosta albo wzgledna, np. '/home/user/projekt'."}},
        ["path"],
    ),
    _tool(
        "git_command",
        "Operacja Git w repozytorium: status, log, diff, branch, show (odczyty) albo pull, commit, push, "
        "checkout, merge, stash pop (wymagaja potwierdzenia).",
        {
            "repo_path": {"type": "string", "description": "Sciezka hosta do repozytorium, np. /srv/shop."},
            "subcommand": {"type": "string",
                           "description": "Podkomenda bez 'git', np. 'status', 'log --oneline -20', 'pull'."},
            "requires_confirmation": _CONFIRM_FLAG,
        },
        ["repo_path", "subcommand"],
    ),
    _tool(
        "system_stats",
        "Stan hosta prosto z /proc hosta: uptime, load average vs liczba rdzeni, RAM, swap, zajetosc "
        "wszystkich dyskow. stat_type=processes dodaje najciezsze procesy.",
        {
            "stat_type": {
                "type": "string",
                "enum": ["summary", "processes", "cpu", "memory", "all"],
                "description": "Zakres danych (domyslnie summary).",
            }
        },
    ),
    _tool(
        "docker_manage",
        "Kontenery i obrazy Docker na hoscie. Odczyty: ps, logs, inspect, stats, top, images, compose-ps, "
        "compose-logs, networks, volumes, df. Zmiany (restart, stop, start, rm, rmi, prune) wymagaja potwierdzenia.",
        {
            "operation": {
                "type": "string",
                "enum": ["ps", "logs", "inspect", "stats", "top", "restart", "stop", "start", "rm", "rmi",
                         "images", "prune", "compose-ps", "compose-logs", "networks", "volumes", "df"],
                "description": "Operacja Docker.",
            },
            "target": {"type": "string",
                       "description": "Kontener/obraz (dla compose-logs: nazwa projektu). Niepotrzebne dla ps, images."},
            "options": {"type": "string", "description": "Dodatkowe flagi, np. '--tail 50' dla logs, '-a' dla ps."},
            "requires_confirmation": _CONFIRM_FLAG,
        },
        ["operation"],
    ),
    _tool(
        "network_info",
        "Diagnostyka sieci hosta: nasluchujace porty (z oznaczeniem publicznych), polaczenia, ping, curl, DNS.",
        {
            "check_type": {
                "type": "string",
                "enum": ["ports", "connections", "listeners", "ping", "curl", "dns"],
                "description": "ports/listeners -- porty hosta, connections -- polaczenia TCP, "
                               "ping/curl/dns -- test celu.",
            },
            "target": {"type": "string",
                       "description": "Dla ping/curl/dns, np. 'example.com', 'http://localhost:8080/health'."},
        },
        ["check_type"],
    ),
    _tool(
        "cron_manage",
        "Zadania cron hosta: list (crontaby i /etc/cron.d), check-logs, add/remove (tylko w trybie native, "
        "z potwierdzeniem). Zadania, ktore ma wykonywac agent, zakladaj jako rutyny (routine_manage).",
        {
            "operation": {"type": "string", "enum": ["list", "add", "remove", "check-logs"]},
            "schedule": {"type": "string", "description": "Dla add: harmonogram cron, np. '0 3 * * *'."},
            "command": {"type": "string",
                        "description": "Dla add: komenda; dla remove: dokladna linia crontaba do usuniecia."},
        },
        ["operation"],
    ),
    _tool(
        "diagram",
        "Rysuje diagram i wysyla go uzytkownikowi jako obraz (Telegram: zdjecie, CLI: plik PNG + podglad w "
        "terminalu). mode=infra: automatyczna mapa infrastruktury hosta (kontenery, projekty compose, reverse "
        "proxy i domeny, porty, uslugi, klaster Kubernetes, zdalne cele) — uzyj, gdy uzytkownik pyta o "
        "architekture albo 'co tu jest postawione'. mode=mermaid: Twoj wlasny diagram w skladni Mermaid "
        "(flowchart, sequenceDiagram, erDiagram, stateDiagram...) — dla przeplywow, procedur i zaleznosci.",
        {
            "mode": {"type": "string", "enum": ["infra", "mermaid"]},
            "mermaid": {"type": "string",
                        "description": "Dla mode=mermaid: kod diagramu Mermaid (bez otoczki ```). Etykiety "
                                       "ze znakami specjalnymi w cudzyslowie: A[\"nginx :443\"]."},
            "title": {"type": "string", "description": "Tytul diagramu (krotki)."},
            "include_kubernetes": {"type": "boolean",
                                   "description": "Dla mode=infra: dolacz aplikacje z klastra (kubectl)."},
        },
        ["mode"],
    ),
    _tool(
        "server_history",
        "Pamiec serwera w czasie (same odczyty, bez LLM). changes: co sie zmienilo na hoscie w podanym okresie "
        "— pakiety, kontenery i obrazy, porty, uslugi systemd, cron, konta, klucze SSH, konfiguracje (nginx, "
        "sshd, sudoers, compose), restart serwera — z przedzialem czasu kazdej zmiany. UZYJ NAJPIERW przy "
        "awarii ('przestalo dzialac', 'od wczoraj'). chart: wykres load/RAM/dyskow z historii czuwania, "
        "wysylany uzytkownikowi jako obraz. checks: waznosc certyfikatow TLS i odpowiedz domen z konfiguracji "
        "proxy, rekordy DNS, swiezosc backupow z DIRECTORY. incidents: pamiec incydentow — co sie juz zdarzalo, "
        "co ustalono i co pomoglo (uzyj, gdy uzytkownik pyta 'czy to juz bylo').",
        {
            "operation": {"type": "string", "enum": ["changes", "chart", "checks", "incidents"]},
            "since": {"type": "string",
                      "description": "Okres wstecz: '24h' (domyslnie), '3h', '7d'. Dla changes i chart."},
            "metric": {"type": "string", "enum": ["load", "memory", "disk"], "description": "Dla chart."},
        },
        ["operation"],
    ),
    _tool(
        "security_audit",
        "Audyt bezpieczenstwa hosta z ocena 0-100: logowanie SSH haslem/rootem, zapora, bazy danych i API Dockera "
        "wystawione publicznie, kontenery uprzywilejowane i z docker.sock, konta z uid 0 i bez hasla, "
        "automatyczne aktualizacje, fail2ban, pliki .env czytelne dla wszystkich, certyfikaty. Kazde znalezisko "
        "ma poprawke z dokladna komenda. Same odczyty. Uzyj, gdy uzytkownik pyta o bezpieczenstwo serwera.",
        {},
    ),
    _tool(
        "mcp_manage",
        "Zewnetrzne serwery MCP, z ktorych korzystasz (ich narzedzia maja nazwy mcp__<serwer>__<narzedzie>). "
        "list: stan serwerow; add: nowy serwer — stdio (command, args, env) albo HTTP (url, headers) — ZAWSZE "
        "z potwierdzeniem; autoApprove to wzorce narzedzi bez pytania, trustReadOnly ufa adnotacji tylko-odczyt; "
        "remove; reload: polacz ponownie.",
        {
            "operation": {"type": "string", "enum": ["list", "add", "remove", "reload"]},
            "name": {"type": "string", "description": "Nazwa serwera: male litery, cyfry, '-', '_'."},
            "command": {"type": "string", "description": "stdio: program, np. 'npx'."},
            "args": {"type": "array", "items": {"type": "string"}, "description": "stdio: argumenty."},
            "env": {"type": "object", "description": "stdio: zmienne srodowiskowe dla serwera."},
            "url": {"type": "string", "description": "HTTP: adres endpointu MCP."},
            "headers": {"type": "object", "description": "HTTP: naglowki (np. Authorization)."},
            "autoApprove": {"type": "array", "items": {"type": "string"},
                            "description": "Wzorce nazw narzedzi wywolywanych bez pytania, np. ['get_*', 'list_*']."},
            "trustReadOnly": {"type": "boolean"},
            "description": {"type": "string"},
        },
        ["operation"],
    ),
    _tool(
        "journal",
        "Dziennik zatwierdzonych zmian: przed kazda zmiana Pipe robi kopie plikow, stanu gita i crontaba oraz "
        "zapisuje komendy odwrotne (docker start/stop, systemctl enable/disable). list: ostatnie wpisy. "
        "undo: cofniecie wpisu (domyslnie ostatniego) — zawsze z potwierdzeniem. Uzyj, gdy uzytkownik mowi "
        "'cofnij', 'przywroc jak bylo', albo gdy zmiana okazala sie bledna.",
        {
            "operation": {"type": "string", "enum": ["list", "undo"]},
            "id": {"type": "string", "description": "Dla undo: identyfikator wpisu (np. 3f2a9c1d); pusty = ostatni."},
        },
        ["operation"],
    ),
    _tool(
        "server_md",
        "Twoja trwala pamiec o tym serwerze: plik SERVER.md, dolaczany do kazdej rozmowy. Zapisuj trwale fakty: "
        "system, uslugi, kontenery, domeny, porty, decyzje uzytkownika. NIGDY sekretow. "
        "Operacje: read; update_section (zastap albo dodaj jedna sekcje '## ...' — preferowane); write (caly plik).",
        {
            "operation": {"type": "string", "enum": ["read", "update_section", "write"]},
            "section": {"type": "string", "description": "Tytul sekcji bez '##' (dla update_section)."},
            "content": {"type": "string", "description": "Markdown: tresc sekcji albo calego pliku."},
        },
        ["operation"],
    ),
    _tool(
        "directory",
        "DIRECTORY: mapa waznych miejsc na serwerze — repozytoria, katalogi aplikacji, projekty compose, "
        "konfiguracje, dane, logi, backupy — z jednozdaniowym opisem. Jest w system prompcie. "
        "upsert dodaje/aktualizuje wpis, scan odkrywa repozytoria git i projekty compose automatycznie.",
        {
            "operation": {"type": "string", "enum": ["list", "upsert", "remove", "scan"]},
            "path": {"type": "string", "description": "Sciezka hosta, np. /srv/shop."},
            "kind": {"type": "string",
                     "enum": ["repo", "app", "compose", "config", "data", "logs", "backup", "other"]},
            "description": {"type": "string", "description": "Jedno zdanie: co to jest i do czego sluzy."},
            "remote": {"type": "string", "description": "Dla repo: adres remote (bez tokenow)."},
            "branch": {"type": "string", "description": "Dla repo: glowna galaz."},
        },
        ["operation"],
    ),
    _tool(
        "skill_manage",
        "Skille to zapisane przez Ciebie procedury wielokrotnego uzytku (np. wdrozenie aplikacji, odnowienie "
        "certyfikatu). Lista jest w system prompcie — zanim wykonasz pasujace zadanie, wczytaj skill (read). "
        "Zapisz skill (save) po wieloetapowej procedurze, ktora sie powtorzy. Skill nie omija zasad bezpieczenstwa.",
        {
            "operation": {"type": "string", "enum": ["list", "read", "save", "delete"]},
            "name": {"type": "string", "description": "Nazwa: male litery, cyfry i myslniki, np. 'odnow-certyfikat'."},
            "description": {"type": "string", "description": "Dla save: jedno zdanie — kiedy uzyc tego skilla."},
            "content": {"type": "string", "description": "Dla save: Markdown z krokami, komendami i weryfikacja."},
        },
        ["operation"],
    ),
    _tool(
        "vibe",
        "VIBE: notatka o tym, jak rozmawiac z biezacym uzytkownikiem (ton, dlugosc, forma, poziom techniczny). "
        "Aktualizuje sie tez sama w tle. Uzyj update, gdy uzytkownik wprost powie, jak mam do niego mowic — "
        "podaj cala nowa notatke (Markdown, zaczyna sie od '# VIBE', maks. 12 punktow).",
        {
            "operation": {"type": "string", "enum": ["read", "update"]},
            "content": {"type": "string", "description": "Dla update: pelna nowa tresc notatki."},
        },
        ["operation"],
    ),
    _tool(
        "target_manage",
        "Zdalne cele: serwery SSH, kontenery Docker, klastry i pody Kubernetes, na ktorych mozesz wykonywac "
        "komendy (remote_exec) i wysylac workerow (delegate). Nic nie jest instalowane po drugiej stronie. "
        "add wymaga potwierdzenia; test sprawdza lacznosc. Cel 'local' (ten host) istnieje zawsze.",
        {
            "operation": {"type": "string", "enum": ["list", "add", "remove", "test"]},
            "name": {"type": "string", "description": "Nazwa celu: male litery, cyfry, '-', '_' (np. 'web-2')."},
            "kind": {"type": "string", "enum": ["ssh", "docker", "kubernetes"]},
            "description": {"type": "string", "description": "Co to za maszyna/klaster."},
            "host": {"type": "string", "description": "ssh: adres hosta."},
            "user": {"type": "string", "description": "ssh: uzytkownik."},
            "port": {"type": "integer", "description": "ssh: port (domyslnie 22)."},
            "identity_file": {"type": "string", "description": "ssh: sciezka klucza w kontenerze Pipe (opcjonalnie)."},
            "container": {"type": "string", "description": "docker: nazwa kontenera."},
            "context": {"type": "string", "description": "kubernetes: kontekst kubeconfig (opcjonalnie)."},
            "namespace": {"type": "string", "description": "kubernetes: namespace (opcjonalnie)."},
            "pod": {"type": "string", "description": "kubernetes: pod dla kubectl exec (puste = caly klaster)."},
            "pod_container": {"type": "string", "description": "kubernetes: kontener w podzie (opcjonalnie)."},
        },
        ["operation"],
    ),
    _tool(
        "remote_exec",
        "Wykonuje jedna komende na zdalnym celu. Klasyfikacja bezpieczenstwa jak w execute_command (dla komendy "
        "docelowej). Na celu-klastrze Kubernetes komenda zaczyna sie od kubectl albo helm.",
        {
            "target": {"type": "string", "description": "Nazwa celu (target_manage list) albo 'local'."},
            "command": {"type": "string", "description": "Komenda do wykonania na celu."},
        },
        ["target", "command"],
    ),
    _tool(
        "delegate",
        "Wysyla workerow — pod-agentow, ktorzy rownolegle badaja cele i zwracaja Ci raporty. Kazdy worker ma "
        "jeden cel i jedno zadanie, wykonuje tylko odczyty, a komendy zmieniajace stan zwraca jako propozycje. "
        "Uzyj dla kilku celow naraz albo dluzszej diagnozy (logi, przyczyna awarii). Podanie nazwy workera "
        "uzytej wczesniej w tej rozmowie kontynuuje jego watek.",
        {
            "tasks": {
                "type": "array",
                "description": "Lista zadan (maks. kilka).",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Nazwa celu albo 'local'."},
                        "task": {"type": "string",
                                 "description": "Konkretne zadanie: co sprawdzic, czego szukac, co ma byc w raporcie."},
                        "name": {"type": "string", "description": "Opcjonalna nazwa workera."},
                    },
                    "required": ["target", "task"],
                },
            }
        },
        ["tasks"],
    ),
    _tool(
        "routine_manage",
        "Rutyny: zadania, ktore wykonujesz sam wedlug harmonogramu (cron) i raportujesz uzytkownikowi — np. "
        "'codziennie o 7 sprawdz backupy i waznosc certyfikatow'. Wykonuje je worker (tylko odczyty). "
        "add wymaga potwierdzenia; run uruchamia rutyne od razu.",
        {
            "operation": {"type": "string", "enum": ["list", "add", "remove", "enable", "disable", "run"]},
            "name": {"type": "string", "description": "Nazwa rutyny, np. 'poranny-przeglad'."},
            "schedule": {"type": "string",
                         "description": "Cron (5 pol, czas serwera), np. '0 7 * * *', albo @hourly/@daily/@weekly."},
            "task": {"type": "string", "description": "Co sprawdzic i co ma byc w raporcie."},
            "target": {"type": "string", "description": "Cel (domyslnie local)."},
            "notify": {"type": "string", "enum": ["always", "problems"],
                       "description": "Raport zawsze albo tylko przy problemie."},
        },
        ["operation"],
    ),
    _tool(
        "reminder",
        "Przypomnienia: JEDNORAZOWA wiadomosc albo zadanie o okreslonym czasie — 'napisz do mnie za 10 sekund', "
        "'przypomnij jutro o 9 o odnowieniu domeny', 'za godzine sprawdz, czy backup sie skonczyl'. To jedyny "
        "sposob, zeby odezwac sie do uzytkownika pozniej: sam nie potrafisz czekac. kind=message wysyla tresc "
        "(bez potwierdzenia); kind=task uruchamia o czasie workera (tylko odczyty) i przysyla raport (wymaga "
        "potwierdzenia). Zadania cykliczne zakladaj jako rutyny (routine_manage).",
        {
            "operation": {"type": "string", "enum": ["add", "list", "cancel"]},
            "delay": {"type": "string",
                      "description": "Dla add: za ile, np. '10s', '15m', '2h', '1d', '1h30m'. Uzywaj, gdy "
                                     "uzytkownik podaje czas wzgledny ('za 10 sekund')."},
            "at": {"type": "string",
                   "description": "Dla add zamiast delay: czas serwera 'HH:MM' (dzis albo jutro) lub "
                                  "'RRRR-MM-DD HH:MM'. Aktualny czas serwera zwraca operation=list."},
            "text": {"type": "string",
                     "description": "Dla add: tresc przypomnienia napisana tak, jak ma ja przeczytac uzytkownik; "
                                    "dla kind=task: co sprawdzic i co ma byc w raporcie."},
            "kind": {"type": "string", "enum": ["message", "task"], "description": "Domyslnie message."},
            "target": {"type": "string", "description": "Dla kind=task: cel (domyslnie local)."},
            "id": {"type": "string", "description": "Dla cancel: identyfikator przypomnienia z list."},
        },
        ["operation"],
    ),
]
