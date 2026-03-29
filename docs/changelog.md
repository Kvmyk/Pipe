# Historia zmian -- Pipe

## v0.2 (2026-03-29)

### Nawigacja po serwerze
- Agent sledzi swoj stan z katalogiem roboczym na hoście (wlasciwosc `cwd` w `Session`)
- Nowe narzedzie `change_directory` umozliwiajace latwe zmienianie polozenia `/hostfs/...`
- Automatyczne powiadamianie uzytkownika o aktualnym polozeniu (tag `[Katalog: ...]`)

### Narzedzia systemowe
- Zainstalowanie w obrazie `git`, `dnsutils`, `iputils-ping` poprawiajace dzialanie `network_info` i `git_command`

### Dokumentacja
- Przeredagowano README odrzucajac zbedny diagram i dodajac wzmianke o multiplatformowosci (Telegram/Discord/CLI)
- Uaktualniono w calym repozytorium wersje do v0.2

---

## v0.1 (2026-03-29)

Pierwsza wersja Pipe -- autonomiczny agent AI do zarzadzania serwerem VPS.

### Narzedzia agenta

- `execute_command` -- wykonywanie komend shell na serwerze
- `read_file` / `write_file` -- odczyt i zapis plikow
- `git_command` -- zarzadzanie repozytoriami Git (status, log, diff, commit, push, stash)
- `system_stats` -- szczegolowe statystyki systemowe z /proc (CPU, RAM, dysk, procesy)
- `docker_manage` -- zarzadzanie kontenerami i obrazami Docker (ps, logs, inspect, stats, restart, stop, prune)
- `network_info` -- diagnostyka sieciowa (otwarte porty, polaczenia, ping, curl, DNS)
- `cron_manage` -- zarzadzanie zadaniami cron (lista, dodawanie, usuwanie, logi)

### Interfejsy

- CLI -- interaktywny terminal z automatycznym tunelem SSH
- Telegram Bot -- zarzadzanie serwerem przez Telegram (HTML formatting)
- Discord -- placeholder (PR welcome)
- Web UI -- placeholder (PR welcome)

### Bezpieczenstwo

- Trzystopniowa klasyfikacja komend: safe / confirm / forbidden
- Append-only audit log
- Izolacja w kontenerze Docker
- Whitelist uzytkownikow Telegram
- Read-only montowanie hosta pod /hostfs

### Backend

- Petla LLM z tool calling (max 10 iteracji)
- Wsparcie dla wielu providerow LLM (Gemini, OpenAI, Groq, Anthropic)
- Unix socket + TCP listener
- Zarzadzanie sesjami per uzytkownik

### Formatowanie Telegram

- Przejscie z MarkdownV2 na HTML parse mode
- Eliminacja problemow z escapowaniem znakow specjalnych
- Automatyczna konwersja resztek Markdowna na HTML
- Fallback na plain text przy bledach parsowania

### Statystyki systemowe

- Zuzycie RAM z /proc/meminfo (nie z uproszczonego `free`)
- Obciazenie CPU z /proc/stat i /proc/loadavg
- Top 10 procesow po CPU/RAM
- Zuzycie dysku (wlaczajac montowanie hosta)

### Dokumentacja

- Struktura docs/ z oddzielnymi plikami dla kazdego komponentu
- Brak emotikon w dokumentacji
- Poprawiona skladnia i gramatyka
