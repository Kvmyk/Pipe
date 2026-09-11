# Historia zmian -- PipeClaw

## v0.5.0 (2026-09-11)

### Providerzy LLM

- Dodano kreator konfiguracji `python3 -m backend.configure`: wybor providera, klucz API, lista modeli pobierana na zywo z API providera i test tool callingu przed zapisaniem `.env`. Kreator nie wymaga instalowania zadnych pakietow
- Dodano 14 wbudowanych providerow: Google Gemini, OpenAI, Anthropic Claude, OpenRouter, Groq, DeepSeek, Mistral, xAI Grok, Z.ai (GLM), Moonshot Kimi, Together AI, Cerebras, Fireworks i lokalna Ollama
- Wlasnego providera (dowolny endpoint zgodny z OpenAI: vLLM, LM Studio, LiteLLM, proxy firmowe) mozna dodac kreatorem -- trafia do `backend/data/providers.json`. Wpis o id wbudowanego providera nadpisuje go, wiec nieaktualny adres da sie poprawic bez nowej wersji PipeClaw
- Nowe zmienne: `LLM_PROVIDER`, `LLM_REASONING_EFFORT`, `LLM_TIMEOUT`. Klucz mozna tez podac przez zmienna providera, np. `OPENAI_API_KEY` albo `GEMINI_API_KEY`
- Dodano tryby `--check` (test obecnej konfiguracji), `--models` (aktualne modele) i `--providers`

### Aktualne modele

- Zmieniono domyslny model z `gemini-2.0-flash` na `gemini-3.8-flash`. Google wylaczyl `gemini-2.0-flash`, przez co swieza instalacja z domyslnym `.env` nie dzialala
- Backend przy starcie sprawdza w tle, czy skonfigurowany model jest nadal dostepny u providera, i wypisuje w logach ostrzezenie z propozycjami, jesli zostal wycofany
- Podniesiono domyslny limit czasu odpowiedzi LLM z 60 do 120 sekund -- modele rozumujace potrafia odpowiadac dluzej

### Zgodnosc

- Stare pliki `.env` (tylko `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`) dzialaja dalej -- provider jest rozpoznawany po adresie. Jesli masz w nim `LLM_MODEL=gemini-2.0-flash`, uruchom `python3 -m backend.configure`
- Agent nie wysyla juz parametru `tool_choice` -- przy podanych narzedziach i tak domyslnie jest `auto`, a czesc providerow (np. Ollama) go nie obsluguje
- W `docker-compose.yml` dodano `host.docker.internal` (dostep do Ollamy na hoscie) i przekazywanie nowych zmiennych

### Dokumentacja i testy

- Przepisano sekcje providerow w `docs/backend.md`; instalacja w `README.md` i `docs/quickstart.md` zaczyna sie od kreatora
- Dodano 57 testow (rejestr providerow, rozwiazywanie konfiguracji, kreator); lacznie 151

---

## v0.4.1 (2026-09-09)

### Odpowiedzi agenta

- Handlery narzedzi nie wysylaja juz do uzytkownika surowego outputu komend. Wczesniej ten sam wynik trafial dwa razy: raz jako surowy STDOUT z kodem wyjscia, raz jako odpowiedz sformulowana przez model -- mimo ze system prompt wprost zabrania modelowi zwracania surowego outputu
- Do uzytkownika trafiaja teraz wylacznie komunikaty protokolu (`[POTWIERDZ]`, `[ODMOWA]`) oraz odpowiedz modelu; pelny wynik komendy nadal dostaje model
- Odmowa dostepu w `read_file` jest zglaszana uzytkownikowi w formacie `[ODMOWA]`, spojnie z `write_file`

### Poprawki

- `system_stats` dla konkretnego typu statystyki (`cpu`, `memory`, `disk`, `process`) przekazywal modelowi jedynie komunikat "Statystyki pobrane" bez danych -- model dostaje teraz faktyczny odczyt
- `change_directory` nie raportuje juz "Katalog zmieniony" po nieudanej zmianie katalogu; do modelu trafia rzeczywisty wynik operacji

---

## v0.4.0 (2026-09-09)

### Naprawa tool callingu

- Naprawiono krytyczny blad: handlery w `backend/core/handlers/` odwolywaly sie do nieistniejacego `agent._executor`, przez co **kazde** wywolanie narzedzia przez LLM konczylo sie bledem. Handlery korzystaja teraz bezposrednio z modulu `backend.core.executor`
- Przywrocono prefiks `/hostfs` przy katalogu roboczym komend i operacji Git -- po refaktorze komendy uruchamialy sie w sciezce kontenera zamiast w sciezce hosta VPS
- Potwierdzone operacje `git_command`, `docker_manage` i `cron_manage` nie koncza sie juz komunikatem "Nieznana operacja" -- `_execute_tool_confirmed` obsluguje kazde narzedzie przechowujace gotowa komende shell

### Autoryzacja tokenem

- `AGENT_TOKEN` faktycznie dziala: brakowalo definicji w `config/settings.py`, wiec dotychczasowa kontrola tokenu byla martwa
- Token jest sprawdzany przed kazda akcja, takze przed potwierdzeniem operacji -- wczesniej nieuwierzytelniony klient mogl zatwierdzic komende oczekujaca w cudzej sesji
- CLI przyjmuje token przez `--token` lub zmienna `AGENT_TOKEN`
- Bot Telegrama przyjmuje token przez `AGENT_TOKEN` w `clients/telegram/.env`
- `AGENT_TOKEN` przekazywany jest do obu serwisow w `docker-compose.yml`

### Wersjonowanie

- Dodano plik `VERSION` w korzeniu repo jako jedyne zrodlo prawdy o wersji
- Dodano `scripts/bump_version.py` -- podbija wersje we wszystkich miejscach, gdzie byla wpisana na sztywno
- Ujednolicono format wersji do `X.Y.Z` (`v0.3` -> `v0.3.0`)
- Dodano skill `/ship` (`.claude/skills/ship/SKILL.md`) opisujacy proces wydania: wersja, changelog, dokumentacja, testy, commit, push

### Testy

- Naprawiono `backend/tests/test_executor.py`, ktory importowal nieistniejaca klase `LocalExecutor` i blokowal zbieranie calego zestawu testow
- Poprawiono bledna asercje w tescie nieistniejacej komendy (powloka zwraca 127 i "command not found")
- Caly zestaw: 94 testy przechodza

### Dokumentacja

- Przepisano `CLAUDE.md`: poprawiono opis protokolu JSON, dodano opis `core/handlers/`, `core/session.py`, konwencji `/hostfs` oraz kontraktu handlerow
- Uzupelniono `docs/protocol.md`, `docs/backend.md`, `docs/cli.md`, `docs/telegram.md` i `README.md` o autoryzacje tokenem
- Poprawiono tabele modulow w `README.md` (`core/executor.py` nie zawiera klasy `LocalExecutor`)

---

## v0.3 (2026-06-19)

### Naprawa statystyk systemowych

- Naprawiono krytyczny blad: statystyki systemu (RAM, CPU, uptime, load average) byly odczytywane z maszyny fizycznej hostujucej VPS zamiast z samego VPS-a
- Usunieto mechanizm `nsenter` ktory uciekal z izolacji LXC VPS-a na maszyne fizyczna
- Usunieto stary mechanizm "teleportacji" statystyk przez cron do `/tmp/vps_*`
- Statystyki sa teraz odczytywane bezposrednio z `/hostproc` (zamontowany `/proc` hosta VPS)

### Konfiguracja Docker

- Dodano montowanie `/proc:/hostproc:ro` w docker-compose dla dostepu do procfs hosta VPS
- Dodano `pid: host` do kontenera vps-agent -- wspoldzielenie PID namespace z VPS-em zapewnia prawidlowe odczyty uptime i loadavg
- Zaktualizowano fake `free` w Dockerfile aby czytal z `/hostproc/meminfo`

### System prompt

- Zaktualizowano system prompt agenta -- usinieto przestarzale odniesienia do teleportacji statystyk
- Dodano informacje o nowej architekturze `/hostproc` i `pid: host`

### Dokumentacja

- Uaktualniono w calym repozytorium wersje do v0.3

---

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

Pierwsza wersja PipeClaw -- autonomiczny agent AI do zarzadzania serwerem VPS.

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
