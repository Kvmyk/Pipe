# Historia zmian -- Pipe

## v0.8.0 (2026-09-11)

### Komendy "/" w Telegramie i CLI

- Dodano `/server`: pokazuje SERVER.md, a gdy go nie ma -- agent bada serwer i tworzy plik. `/server aktualizuj` bada serwer ponownie
- Dodano `/skille` (lista skilli) i `/pomoc` (lista komend)
- Kazdy skill ma wlasna komende, np. `/odnow_certyfikat`; do komendy mozna dopisac wskazowki (`/odnow_certyfikat tylko dla example.com`). W CLI dziala tez forma z myslnikiem
- Telegram: bot sam ustawia menu podpowiedzi po wpisaniu `/` i odswieza je po kazdej wiadomosci, wiec nowy skill pojawia sie w menu od razu. Menu jest ustawiane osobno dla czatu kazdego dozwolonego uzytkownika -- osoby spoza listy nie widza nazw ani opisow skilli
- Telegram: na nieznana komende bot odpowiada podpowiedzia (wczesniej milczal)
- CLI: tekst zaczynajacy sie od `/`, ktory nie jest komenda (np. `/var/log jest pelny?`), nadal trafia do agenta jak zwykla wiadomosc

### Protokol

- Nowy typ zadania `{"command": ...}`: `list_skills`, `server_md`, `scan_server`, `run_skill`. Zmiana jest addytywna -- dotychczasowe klienty dzialaja bez zmian. Tresc wiadomosci dla agenta buduje backend, nie klient

### Poprawki

- Nazwa skilla to zawsze nazwa jego katalogu -- reczna edycja naglowka w SKILL.md nie psuje juz komendy ani odczytu

### Dokumentacja

- Opisano komendy w `docs/telegram.md` i `docs/cli.md`, nowy typ zadania w `docs/protocol.md`
- Dodano instrukcje korzystania z CLI bezposrednio na serwerze (`--no-tunnel`)
- Dodano 35 testow (komendy w backendzie i klientach); lacznie 332

---

## v0.7.0 (2026-09-11)

### Pamiec agenta

- Agent prowadzi wlasny `SERVER.md` -- notatki o serwerze (system, uslugi, kontenery, domeny, porty, wazne sciezki, decyzje uzytkownika), dolaczane do kazdej rozmowy. Nowe narzedzie `server_md`: odczyt, aktualizacja jednej sekcji i zapis calosci
- Agent moze tworzyc wlasne skille -- zapisane procedury wielokrotnego uzytku w formacie Agent Skills (`skills/<nazwa>/SKILL.md`). W prompcie jest tylko lista skilli, a tresc agent wczytuje, gdy zadanie pasuje do opisu. Nowe narzedzie `skill_manage`: lista, odczyt, zapis i usuwanie
- Pamiec lezy na serwerze w `backend/data/` (w kontenerze `/app/data`, zmienna `DATA_DIR`) i przetrwa restart oraz przebudowe obrazu. Katalog dodano do `.gitignore`
- Zapis do pamieci nie wymaga potwierdzenia, ale trafia do audit logu (sama sciezka, bez tresci). Zapis wygladajacy na sekret (klucz prywatny, klucz API, token, `DB_PASSWORD=...`) jest odrzucany, bo `SERVER.md` jest wysylany do providera LLM z kazdym zapytaniem
- Pamiec jest w prompcie oznaczona jako dane, nie polecenia -- nie zmienia klasyfikacji komend ani wymogu potwierdzen

### Dokumentacja

- Usunieto wzmianki o konkretnym dostawcy VPS -- dokumentacja, przyklady i instalatory mowia teraz po prostu o serwerze (przykladowy adres: `serwer.example.com`)
- Opisano pamiec agenta w `README.md`, `docs/backend.md` i `docs/security.md`
- Dodano 64 testy (pamiec agenta, kazde narzedzie ma handler); lacznie 297

---

## v0.6.0 (2026-09-11)

### Zmiana nazwy

- Projekt nazywa sie teraz **Pipe** -- nowa nazwa jest w dokumentacji, komunikatach, prompcie systemowym agenta, bocie Telegrama, kreatorze konfiguracji i banerze CLI
- Komenda CLI to teraz `pipe`. Instalatory (`install.sh`, `install.ps1`) przy ponownym uruchomieniu zastepuja w profilu powloki blok z poprzednia nazwa komendy, zamiast dopisywac drugi. Kto nie uruchomi instalatora ponownie, zachowa dzialajaca poprzednia komende
- Agent przedstawia sie teraz jako Pipe

### Dokumentacja

- Wyrownano ramke przykladowej sesji w `docs/quickstart.md`

---

## v0.5.1 (2026-09-11)

### Potwierdzenia operacji

- Naprawiono blad: w Telegramie potwierdzenie moglo pokazywac inna komende niz ta, ktora zostanie wykonana. Gwiazdki, podkreslniki i backslashe byly zamieniane na formatowanie (`rm /var/log/*.gz /tmp/*.old` wyswietlalo sie jako `rm /var/log/.gz /tmp/.old`), a komenda z `<` byla ucinana (`mysql produkcja < drop_all.sql` wyswietlalo sie jako `mysql produkcja`)
- Komendy w potwierdzeniach i odmowach sa pokazywane znak w znak -- w Telegramie i w CLI -- takze gdy zawieraja backticki (np. podstawienie komendy) albo kilka linii
- Potwierdzenie operacji Docker pokazuje pelna komende `docker ...`, ktora zostanie wykonana
- Bot wysyla poprawny HTML Telegrama: `<`, `>` i `&` poza tagami sa escapowane, wiec wiadomosc nie jest odrzucana
- Tryb awaryjny (zwykly tekst) usuwa juz tylko prawdziwe tagi, a nie wszystko miedzy `<` a `>`

### Cron

- Usunieto niebezpieczna sciezke w `cron_manage`: "usuniecie wpisu" zapisywalo do wykonania `crontab -r`, czyli wyczyszczenie calego crontaba, a od v0.4.0 taka komenda byla wykonywana po zatwierdzeniu. W praktyce sciezka byla nieosiagalna (patrz ograniczenie nizej). Usuwanie dotyczy teraz wylacznie wskazanej linii, dodawanie dopisuje wpis do istniejacego crontaba, wpis jest bezpiecznie cytowany, a zabronione polecenia w nim sa odrzucane
- Znane ograniczenie: `cron_manage` nadal nie dziala -- handler czyta inne nazwy parametrow niz te, ktore wysyla model, a obraz Dockera nie zawiera programu `crontab`

### Wewnetrzne

- Formatowanie odpowiedzi dla Telegrama wydzielono do `clients/telegram/tg_format.py` (testowalne bez biblioteki Telegrama)
- Dodano 82 testy (formatowanie w Telegramie i CLI, cron); lacznie 233

---

## v0.5.0 (2026-09-11)

### Providerzy LLM

- Dodano kreator konfiguracji `python3 -m backend.configure`: wybor providera, klucz API, lista modeli pobierana na zywo z API providera i test tool callingu przed zapisaniem `.env`. Kreator nie wymaga instalowania zadnych pakietow
- Dodano 14 wbudowanych providerow: Google Gemini, OpenAI, Anthropic Claude, OpenRouter, Groq, DeepSeek, Mistral, xAI Grok, Z.ai (GLM), Moonshot Kimi, Together AI, Cerebras, Fireworks i lokalna Ollama
- Wlasnego providera (dowolny endpoint zgodny z OpenAI: vLLM, LM Studio, LiteLLM, proxy firmowe) mozna dodac kreatorem -- trafia do `backend/data/providers.json`. Wpis o id wbudowanego providera nadpisuje go, wiec nieaktualny adres da sie poprawic bez nowej wersji Pipe
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
