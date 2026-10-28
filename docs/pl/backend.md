# Backend -- Pipe

Pipe v0.49.2

Backend agenta. Działa na serwerze (Docker, natywnie z systemd albo w Kubernetesie -- [deploy.md](./deploy.md))
i wystawia lokalny Unix socket i port TCP 127.0.0.1:7379 dla klientów.

## Wymagania

- Linux, Python 3.11+
- tryb docker: Docker 24+ z Compose v2 (instalator doinstaluje je z repozytorium dystrybucji)
- tryb native: systemd; tryb kubernetes: klaster z PVC

## Moduly

| Modul | Rola |
|-------|------|
| `server.py` | Unix socket + TCP, ramki JSON lines, komendy klientów, subskrypcja zdarzeń, start czuwania |
| `core/agent.py` | Pętla LLM z tool calling (`AGENT_MAX_ITERATIONS`), potwierdzenia, redakcja i przycinanie wyników, historia |
| `core/handlers/` | Jeden modul na obszar narzędzi; `handle_<nazwa>` = narzędzie `<nazwa>` |
| `core/security.py` | Klasyfikator komend (fail-closed, tokenowy, świadomy cudzysłowów), workspace |
| `core/runtime.py` | Tryb działania (docker / native / kubernetes) i mapowanie ścieżek hosta |
| `core/hostinfo.py` | Stan hosta z `/proc`: RAM, CPU, dyski, gniazda -- bez uruchamiania komend |
| `core/infra.py` | Odkrywanie infrastruktury (Docker, nginx, Caddy, Traefik, systemd, git, Kubernetes) i mapa Mermaid |
| `core/diagram.py` | Mermaid -> PNG + ASCII (mermaidx, offline) |
| `core/targets.py` | Zdalne cele: rejestr, walidacja, budowanie komend ssh / docker exec / kubectl |
| `core/workers.py` | Workery: pod-agenci na pętli agenta, tylko odczyty, równolegle |
| `core/routines.py` | Rutyny: harmonogram cron, rejestr |
| `core/watch.py` | Czuwanie: sprawdzenia, alerty, powiadomienia (pub/sub), uruchamianie rutyn i pętle poniżej |
| `core/metrics.py` | Historia pomiarów (load, RAM, dyski) i wykresy Mermaid `xychart-beta` |
| `core/snapshots.py` | Migawki stanu hosta i różnice -- "co się zmieniło" |
| `core/checks.py` | Sprawdzenia bez konfiguracji: certyfikaty, strony, DNS, świeżość backupów |
| `core/digest.py` | Poranny raport (bez LLM) |
| `core/usage.py` | Licznik tokenów i kosztów LLM, dzienne limity |
| `core/posture.py` | Audyt bezpieczeństwa hosta (ocena, poprawki), strażnik zmian bezpieczeństwa, log SSH |
| `core/vulns.py` | Podatności bez LLM (OSV.dev, CISA KEV): pakiety hosta, obrazy kontenerów, zdalne serwery, aplikacje; raporty i kroki łatek |
| `core/lockfiles.py` | Zależności aplikacji z lockfile'i (npm, yarn, pnpm, PyPI, uv, Go, crates.io, Packagist, RubyGems) |
| `core/patching.py` | `/zalataj`: łatanie w tle bez pytań o każdą komendę, części przez bezpiecznik, warstwa z poprawkami w obrazie, sprzątanie, wynik |
| `core/fleet.py` | Zdalne serwery w skanie i łatkach: baza pakietów przez ssh, komendy jako root albo `sudo -n` |
| `core/welcome.py` | Powitanie po instalacji: mapa, ocena bezpieczeństwa, co Pipe pilnuje |
| `core/feed.py` | Skrzynka: wiadomości, które Pipe wysłał sam, z akcjami; decyzja o powiadomieniu (poziom, cisza nocna, limit, wyciszenia) |
| `core/insights.py` | Podpowiedzi bez LLM: prognoza zapełnienia dysku, miejsce do odzyskania, restarty, czekający restart, rosnący RAM |
| `core/overview.py` | Stan serwera bez LLM (pulpit, `/status`): nagłówek, wskaźniki, przebieg doby |
| `core/cleanup.py` | Zwolnij miejsce: obrazy bez tagu, cache budowania, pobrane paczki (`/sprzataj`, przycisk podpowiedzi) |
| `core/projects.py` | Warsztat: klonowanie, rozpoznanie projektu, plan uruchomienia, piaskownica (`docker run` bez uprawnień) |
| `core/toolsets.py` | Narzędzia na żądanie: grupy narzędzi włączane słowami kluczowymi albo `load_tools` |
| `skills_builtin/`, `skills_builtin_en/` | Wbudowane skille (polskie i angielskie), instalowane do `data/skills` przy starcie według `PIPE_LANG` |
| `core/i18n.py` | Język Pipe (`PIPE_LANG=en\|pl`): `tr("polski", "english")` obok tekstu źródłowego, `prompt()` wybiera `prompts_en.py` / `tools_en.py` |
| `core/selfupdate.py` | Aktualizacja samego Pipe: kontener pomocniczy (`git pull`, obraz wydania z GHCR albo `docker-compose up -d --build`), raport po restarcie |
| `core/reminders.py` | Przypomnienia: jednorazowe wiadomości i zadania o czasie (`reminders.json`), odpala je czuwanie |
| `core/incidents.py` | Pamięć incydentów: alert -> ustalenia z "Zbadaj" -> co pomogło (dziennik) |
| `core/webhooks.py` | Serwer HTTP alertów z zewnątrz (Alertmanager, Grafana, Uptime Kuma, GitHub) |
| `core/voice.py` | Transkrypcja wiadomości głosowych (endpoint Whisper zgodny z OpenAI albo model Gemini z wejściem audio) |
| `core/attachments.py` | Załączniki wiadomości: limity, obraz / tekst / plik binarny po zawartości, `DATA_DIR/uploads/` |
| `tokens.py` | Tokeny klientów z rolami admin/viewer (`python3 -m backend.tokens`) |
| `core/mcp/` | MCP: `server.py` (Pipe jako serwer, obie ery protokołu, Streamable HTTP) |
| `core/hostexec.py` | Tryb docker: komenda na hoście przez kontener pomocniczy (`--privileged --pid=host`, `nsenter -t 1`), po TAK |
| `core/approvals.py` | Zgody administratora dla operacji zewnętrznych agentów |
| `core/auth.py` | Uwierzytelnienie tokenem (JSON lines i MCP HTTP) |
| `core/safety.py` | Bezpiecznik: plan zmiany (kopie, sprawdzenie przed, weryfikacja po) i strzeżone wykonanie |
| `core/journal.py` | Dziennik zmian: kopie plików, stan gita i crontaba, komendy odwrotne, `/cofnij` |
| `core/memory.py` | SERVER.md, DIRECTORY, skille, VIBE, wykrywanie i redakcja sekretów |
| `core/vibe.py` | Nauka stylu rozmowy w tle |
| `core/events.py` | Zdarzenia strumienia: tekst, `Attachment`, `Progress` |
| `core/executor.py` | `execute` (limit czasu, zabijanie grupy procesów), `read_file`, `write_file` |
| `core/audit.py` | Append-only audit log |
| `core/tools.py` | Schematy narzędzi (OpenAI function calling) |
| `config/` | `settings.py` (zmienne `.env`), `providers.py` (presety LLM), `prompts.py` |
| `configure.py` | Kreator providera (`--check`, `--models`, `--providers`, `--from-env`) |

## Konfiguracja

### Skonfiguruj providera LLM

Z katalogu głównego repozytorium uruchom kreator (sam utworzy `backend/.env` z `.env.example`):

```bash
python3 -m backend.configure
```

Kreator:
1. pokaże listę providerów i zapyta o klucz API (z linkiem, gdzie go zdobyć),
2. pobierze **aktualna** listę modeli prosto z API providera -- nowe modele są widoczne od razu, bez aktualizacji Pipe,
3. sprawdzi na żywo, czy wybrany model obsługuje tool calling (bez tego agent nie wykona żadnej komendy),
4. zapisze `backend/.env` (utworzy go z `.env.example`, jeśli nie istnieje; uprawnienia `600`).

Kreator używa wyłącznie biblioteki standardowej Pythona -- nie trzeba niczego instalować.

Przydatne tryby:

```bash
python3 -m backend.configure --check      # test obecnej konfiguracji (lista modeli + tool calling)
python3 -m backend.configure --models     # aktualne modele obecnego providera
python3 -m backend.configure --providers  # wszyscy dostepni providerzy
```

### Wbudowani providerzy

Wszyscy przez API zgodne z OpenAI Chat Completions. Adresy zweryfikowane we wrześniu 2026.

| `LLM_PROVIDER` | Provider | Domyślny model | Uwagi |
|---|---|---|---|
| `gemini` | Google Gemini | `gemini-3.8-flash` | Domyślny. Darmowy tier dla modeli Flash, ale patrz niżej (prywatność) |
| `openai` | OpenAI | `gpt-5.6-terra` | GPT-6 wymaga Responses API do tool callingu -- użyj GPT-5.6 |
| `anthropic` | Anthropic Claude | `claude-sonnet-5` | Przez warstwę zgodności z OpenAI SDK |
| `openrouter` | OpenRouter | `google/gemini-3.8-flash` | Jeden klucz, kilkaset modeli (DeepSeek, Qwen, GLM, Kimi...) |
| `groq` | Groq | `openai/gpt-oss-120b` | Bardzo szybka inferencja |
| `deepseek` | DeepSeek | `deepseek-flash` | |
| `mistral` | Mistral AI | `mistral-large-latest` | Provider z UE |
| `xai` | xAI Grok | `grok-4.6` | |
| `zai` | Z.ai (GLM) | `glm-5.3` | |
| `kimi` | Moonshot Kimi | `kimi-k3` | |
| `together` | Together AI | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | |
| `cerebras` | Cerebras | `gpt-oss-120b` | |
| `fireworks` | Fireworks AI | -- (wybierz z listy) | |
| `ollama` | Ollama (lokalnie) | -- (wybierz z listy) | Bez klucza, patrz niżej |

Domyślny model to tylko punkt startowy. Przy starcie backend sprawdza, czy skonfigurowany model nadal
jest na liście providera, i wypisuje ostrzeżenie z propozycjami, jeśli został wycofany.

Prywatność: Pipe wysyła providerowi logi, konfiguracje i wyniki komend z serwera (sekrety redaguje). W darmowym
tierze Gemini API Google może używać treści zapytań do ulepszania swoich usług i mogą je czytać ludzie. Kreator,
`pipe web` i CLI ostrzegają o tym przy wyborze Gemini. Na serwerze produkcyjnym włącz płatności w Google AI Studio
albo wybierz innego providera, np. lokalna Ollame.

### Bez modelu (`LLM_PROVIDER=none`)

Pipe może działać bez żadnego modelu. Wtedy nic nie wychodzi z serwera i nic nie kosztuje, a dalej działają:
czuwanie i alerty, migawki i „co się zmieniło”, poranny raport, monitoring certyfikatów i stron, audyt
bezpieczeństwa, dziennik i `/cofnij`, diagramy, webhooki (alert dociera, ale nikt go nie bada) oraz bramka MCP
dla innych agentów (bez `ask_pipe`). Rozmowa, rutyny i badanie alertów odpowiadają komunikatem, że modelu nie ma.
Gdy później dodasz providera w `pipe web` albo `/providerzy`, rozmowa włącza się bez restartu.

W kreatorze to ostatnia pozycja listy, a bez pytań: `LLM_PROVIDER=none bash scripts/install-server.sh -y`.

### Ręczna konfiguracja `.env`

Zamiast kreatora możesz edytować `backend/.env` ręcznie:

```env
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_MODEL=            # pusty = domyslny model providera
```

Zamiast `LLM_API_KEY` możesz ustawić zmienna specyficzna dla providera, np. `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `ANTHROPIC_API_KEY` -- `LLM_API_KEY` ma pierwszeństwo.

Provider z `.env` jest **bazowy** -- z nim backend startuje. Kolejnych można dodać bez restartu w `pipe web`
(ekran wyboru providera, `/provider`): klucz trafia do `DATA_DIR/llm_keys.json` (0600), a przełącznik w rozmowie
zmienia providera całego agenta. Zmienne presetów ustawione obok `LLM_API_KEY` (np. `OPENAI_API_KEY`,
`GROQ_API_KEY`) też liczą się jako gotowi providerzy w przełączniku. `WORKER_MODEL`, `LLM_REASONING_EFFORT`
i ceny `LLM_PRICE_*` dotyczą tylko providera bazowego.

Opcjonalnie:

```env
LLM_BASE_URL=https://proxy.example.com/v1   # nadpisuje adres z presetu (np. proxy firmowe)
LLM_REASONING_EFFORT=low                     # dla modeli, ktore obsluguja ten parametr
LLM_TIMEOUT=120                              # limit czasu odpowiedzi LLM w sekundach
```

Stare pliki `.env` (tylko `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`, bez `LLM_PROVIDER`) działają
dalej -- provider jest rozpoznawany po adresie.

### Własny provider

Dowolny endpoint zgodny z OpenAI (vLLM, LM Studio, LiteLLM, firmowe proxy...) dodasz kreatorem --
wybierz opcje **Inny** i podaj adres. Provider zostanie zapisany w `backend/data/providers.json`
i od tej pory jest dostępny jak wbudowany (`LLM_PROVIDER=<id>`).

Możesz też edytować ten plik ręcznie:

```json
{
  "providers": [
    {
      "id": "moj-vllm",
      "name": "Moj vLLM",
      "base_url": "http://192.168.1.10:8000/v1",
      "default_model": "qwen3",
      "requires_key": false
    }
  ]
}
```

Wpis o `id` wbudowanego providera nadpisuje go -- tak poprawisz nieaktualny adres bez czekania na nową
wersję Pipe. Katalog `backend/data/` jest zamontowany w kontenerze, więc zmiany nie wymagają przebudowy
obrazu -- wystarczy `docker-compose restart vps-agent`.

### Ollama (modele lokalne)

Kontener widzi hosta pod adresem `host.docker.internal` (ustawione w `docker-compose.yml`). Ollama domyślnie
nasłuchuje tylko na `127.0.0.1`, więc musisz ją uruchomić z `OLLAMA_HOST=0.0.0.0`, żeby kontener ją widział.
Model musi obsługiwać tool calling -- kreator to sprawdzi.

### Zabezpieczenie tokenem

```env
# Zabezpieczenie (Opcjonalne, ale bardzo zalecane)
# Jesli jest ustawione, kazde zadanie -- takze potwierdzenie operacji -- musi zawierac
# ten token. Chroni socket i interfejs TCP przed innymi procesami na serwerze.
# Ten sam token podaj klientom: CLI przez --token (lub zmienna AGENT_TOKEN),
# botowi Telegrama przez AGENT_TOKEN w clients/telegram/.env.
# Puste = brak wymogu tokenu.
AGENT_TOKEN=twoj-tajny-token
```

## Pamięć agenta: SERVER.md i skille

Agent ma kilka rodzajów trwałej pamięci: SERVER.md, skille (poniżej), a także DIRECTORY (mapa repozytoriów
i katalogów) i VIBE (styl rozmowy) -- te dwie opisuje [features.md](./features.md). Wszystko leży na hoście
w `backend/data/` (w kontenerze `/app/data`, zmienna `DATA_DIR`), przetrwa restart i przebudowe obrazu
i nie trafia do gita. Tam są też `targets.json`, `routines.json`, `reminders.json`, `watch_state.json` i audit log.

### SERVER.md

Notatki agenta o serwerze -- jak `AGENTS.md`, ale dla serwera. Agent aktualizuje je sam (narzędzie `server_md`),
gdy pozna trwały fakt: usługi, kontenery, domeny, porty, ważne ścieżki, Twoje decyzje. Cały plik jest dołączany
do system promptu każdej rozmowy (do 12 000 znaków; sam plik może mieć do 20 000).

Na start możesz poprosić: *"zbadaj serwer i utwórz SERVER.md"*. Plik możesz też edytować ręcznie:
`backend/data/SERVER.md`.

### Skille

Zapisane procedury wielokrotnego użytku, w formacie Agent Skills:

```
backend/data/skills/<nazwa>/SKILL.md
```

```markdown
---
name: odnow-certyfikat
description: Odnowienie certyfikatu TLS dla nginx
---

1. `certbot renew`
2. `nginx -t && systemctl reload nginx`
3. Sprawdz date waznosci certyfikatu.
```

W system prompcie jest tylko lista skilli (nazwa i opis). Pełna treść agent wczytuje narzędziem `skill_manage`,
gdy zadanie pasuje do opisu -- dzięki temu wiele skilli nie zapycha kontekstu. Agent tworzy skill po wykonaniu
wieloetapowej procedury, która się powtórzy, albo na Twoją prośbę.

Każdy skill ma też własną komendę w Telegramie i CLI: myślniki zamieniają się na `_`, a nazwa jest skracana
do 32 znaków (limit Telegrama), np. `odnow-certyfikat` -> `/odnow_certyfikat`. Skill o nazwie zajętej przez
komendę wbudowana (`status`, `server`, `skille`, ...) nie dostaje komendy -- uruchomisz go przez `/skille`
albo zwykłym tekstem.

### Bezpieczeństwo pamięci

- Zapis do pamięci nie wymaga potwierdzenia -- nie zmienia serwera, tylko notatki agenta. Każdy zapis trafia do
  audit logu (sama ścieżka, bez treści).
- Skille i SERVER.md nie omijają zasad bezpieczeństwa: każda komenda z nich nadal przechodzi klasyfikację
  i wymóg potwierdzenia.
- SERVER.md jest wysyłany do providera LLM z każdym zapytaniem, dlatego zapis oczywistych sekretów (klucze
  prywatne, klucze API, tokeny, `haslo=...`) jest odrzucany. Agent zapisuje, *gdzie* sekret jest przechowywany,
  a nie sam sekret.

## Uruchomienie

```bash
docker-compose up -d
```

## Weryfikacja

```bash
docker logs vps-agent --tail 20
```

Powinieneś zobaczyć:
```
[VPS Agent] Unix socket : /tmp/vps-agent.sock
[VPS Agent] TCP         : 0.0.0.0:7379 (tylko localhost)
[VPS Agent] Provider    : Google Gemini (https://generativelanguage.googleapis.com/v1beta/openai/)
[VPS Agent] Model       : gemini-3.8-flash
[VPS Agent] Runtime     : docker (host: /hostfs, proc: /hostproc)
[VPS Agent] Diagramy    : mermaidx
[VPS Agent] Czuwanie    : co 120 s
[VPS Agent] Serwer gotowy. Ctrl+C aby zatrzymac.
```

## Narzędzia agenta

| Narzędzie | Opis | Wymaga potwierdzenia |
|-----------|------|----------------------|
| `execute_command` | Komendy shell; w trybie docker z `host: true` na hoście (kontener pomocniczy) | Zależne od klasyfikacji; `host`: zawsze |
| `read_file` | Odczyt plików (sekrety redagowane) | Nie |
| `write_file` | Zapis plików (tworzy brakujące katalogi) | Tak; w warsztacie (`WORKSPACE_DIR`): nie |
| `change_directory` | Katalog roboczy | Nie |
| `git_command` | Operacje Git | Modyfikujące: tak |
| `system_stats` | CPU/RAM/dyski/procesy z `/proc` hosta | Nie |
| `network_info` | Porty hosta, połączenia, ping, curl, DNS | Nie |
| `cron_manage` | Cron hosta (edycja tylko w trybie native) | Modyfikujące: tak |
| `diagram` | Mapa infrastruktury / własny diagram Mermaid jako obraz | Nie |
| `target_manage` | Rejestr zdalnych celów | Dodanie: tak |
| `remote_exec` | Komenda na zdalnym celu | Zależne od klasyfikacji |
| `delegate` | Workery (tylko odczyty) | Nie -- zmiany wracają jako propozycje |
| `routine_manage` | Rutyny według harmonogramu | Dodanie: tak |
| `reminder` | Jednorazowe przypomnienie albo zadanie o określonym czasie | Zadanie: tak; wiadomość: nie |
| `pipe_update` | Aktualizacja samego Pipe (check / apply) | apply: tak |
| `security_audit` | Audyt bezpieczeństwa z oceną i komendami poprawek | Nie (poprawki: tak) |
| `security_patch` | Łatki podatności jak `/zalataj`: pakiety, obrazy, zdalne serwery, sprzątanie | Raz na całą łatkę (YOLO: nie) |
| `web_search` | Wyszukiwanie w internecie bez klucza (DuckDuckGo → Stack Exchange → Wikipedia) | Nie |
| `web_fetch` | Czytanie strony; z `question` stronę czyta osobny model bez narzędzi | Nie dla adresow z wyników i od użytkownika, inne: tak |
| `software_info` | `eol` (endoflife.date), `vulns` (OSV.dev) | Nie |
| `journal` | Dziennik zatwierdzonych zmian i ich cofanie | Cofnięcie: tak |
| `server_history` | Co się zmieniło, wykresy load/RAM/dyskow, certyfikaty/strony/DNS/backupy | Nie |
| `server_md`, `directory`, `skill_manage`, `vibe` | Pamięć agenta; `skill_manage install` -- skille i pluginy z repozytorium git | Nie; zapis skilla i `install`: tak |
| `project` | Warsztat: `clone`, `inspect`, `exec` (izolowany kontener), `run`, `logs`, `stop`/`start`, `pull`, `remove` | `run` i `remove`: tak; reszta: nie |
| `mcp_manage` | Zewnętrzne serwery MCP: `list`, `add` (izolowany kontener albo HTTP), `remove`, `reload` | `add`: tak |
| `mcp__<serwer>__<narzedzie>` | Narzędzia podłączonych serwerów MCP (grupa `mcp:<serwer>` ładowana na żądanie) | Odczyty (`readOnlyHint`): nie; reszta: tak |
| `load_tools` | Włącza grupę narzędzi, których opisów model jeszcze nie dostał (narzędzia na żądanie) | Nie |

Przy `TOOLS_MODE=lazy` (domyślnie) model dostaje na start tylko podstawowe narzędzia (komendy, pliki, git, stan
systemu, Docker, sieć, historia serwera, `project`, `load_tools`). Grupy `memory`, `security`, `schedule`, `fleet`,
`web`, `diagram` i `update` włączają się same, gdy wiadomość ich potrzebuje, albo przez `load_tools`.

## Dodatkowe ustawienia `.env`

| Zmienna | Domyślnie | Opis |
|---------|-----------|------|
| `PIPE_RUNTIME` | `auto` | `docker` / `native` / `kubernetes` |
| `PIPE_LANG` | `en` | Język agenta: `en` albo `pl` (prompty, opisy narzędzi, komunikaty, alerty, raporty, wbudowane skille, kreator). Bot Telegrama czyta tę samą zmienną ze swojego `.env`, CLI -- `--lang` / `PIPE_LANG` |
| `HOST_ROOT`, `HOST_PROC` | wg trybu | Nadpisanie ścieżek hosta |
| `AGENT_MAX_ITERATIONS` | 15 | Limit kroków pętli na jedna wiadomość |
| `SESSION_KEEP_DAYS` | 7 | Ile dni rozmowy leżą w `DATA_DIR/sessions` (0600) i wracają po restarcie backendu; 0 = bez zapisu |
| `CONFIRMED_COMMAND_TIMEOUT` | 900 | Limit czasu (s) komend zatwierdzonych przez użytkownika; odczyty mają 30 s |
| `REDACT_SECRETS` | 1 | Redakcja sekretów w wynikach narzędzi |
| `WEB_SEARCH` | on | Internet dla agenta (`web_search`, `web_fetch`, `software_info`); `off` wyłącza |
| `WORKER_MODEL` | model agenta | Model workerów i nauki VIBE |
| `WORKER_MAX_ITERATIONS`, `WORKER_TIMEOUT`, `MAX_WORKERS` | 8, 240, 6 | Limity workerów |
| `VIBE_EVERY` | 6 | Co ile wiadomości odświeżać VIBE (0 = wyłączone) |
| `WATCH_ENABLED`, `WATCH_INTERVAL` | 1, 120 | Czuwanie |
| `WATCH_DISK_PCT`, `WATCH_MEM_PCT`, `WATCH_LOAD_FACTOR` | 90, 92, 2 | Progi alertów |
| `METRICS_KEEP_DAYS` | 8 | Jak długo trzymać historię pomiarów (wykresy) |
| `SNAPSHOT_INTERVAL`, `SNAPSHOT_KEEP_DAYS` | 3600, 30 | Migawki stanu hosta ("co się zmieniło") |
| `CHECKS_INTERVAL` | 3600 | Co ile sekund sprawdzać certyfikaty, strony, DNS i backupy |
| `VULN_INTERVAL` | 21600 | Co ile sekund pytać OSV.dev o podatności wszystkich pakietów (zmienione pakiety od razu); 0 = wyłączone |
| `WATCH_SITES` | 1 | 0 wyłącza sprawdzenia sieciowe (certyfikaty, strony, DNS) |
| `WATCH_CERT_DAYS`, `WATCH_BACKUP_HOURS` | 14, 26 | Progi: dni do wygaśnięcia certyfikatu, wiek najnowszego backupu |
| `WATCH_SSH_FAILURES` | 60 | Alert przy tylu nieudanych logowaniach SSH w 10 min (0 = wyłączone) |
| `WATCH_SSH_LOGINS` | 1 | Alert przy logowaniu SSH z nowego adresu |
| `WATCH_IGNORE` | -- | Domeny i ścieżki pomijane w sprawdzeniach (po przecinku) |
| `DIGEST_TIME` | `07:00` | Godzina porannego raportu (czas serwera); `off` wyłącza |
| `LLM_PRICE_IN`, `LLM_PRICE_OUT` | -- | Ceny modelu w USD za milion tokenów -- `/koszt` pokaże koszt |
| `WORKER_PRICE_IN`, `WORKER_PRICE_OUT` | jak LLM | Ceny `WORKER_MODEL` |
| `AGENT_VIEWER_TOKEN` | -- | Token roli viewer (tylko odczyt); wymaga `AGENT_TOKEN` |
| `WEBHOOK_PORT`, `WEBHOOK_HOST`, `WEBHOOK_TOKEN` | 0, 127.0.0.1, -- | Serwer alertów z zewnątrz (0 = wyłączony; bez tokenu nie wystartuje) |
| `WEBHOOK_INVESTIGATE` | 1 | Nowy alert z webhooka bada worker (tylko odczyty), ustalenia z przyciskiem *Napraw*; limit raz na 6 h na alert, 10/dzien |
| `MCP_PORT`, `MCP_HOST`, `MCP_ALLOWED_ORIGINS` | 0, 127.0.0.1, -- | Pipe jako serwer MCP po HTTP (`/mcp`, token Pipe jako Bearer) |
| `MCP_<NAZWA>` (dowolne) | -- | Sekrety dla serwerów MCP, z których korzysta Pipe -- w konfiguracji serwera jako `${MCP_<NAZWA>}` |
| `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL`, `STT_LANGUAGE` | wg providera, język Pipe | Transkrypcja głosu (gemini/openai/groq -- automatycznie; `STT_*` ma pierwszeństwo) |
| `SAFE_AUTO_ROLLBACK` | 1 | Nieudana weryfikacja zmiany plików konfiguracji -> automatyczne przywrócenie kopii |
| `AUTONOMY` | 1 | Rutynowe zmiany bez pytania (przez bezpiecznik), o sprawy ważne Pipe pyta (`docs/security.md`, *Samodzielność*); 0 = pytanie o każdą zmianę |
| `DAILY_TOKEN_LIMIT`, `DAILY_COST_LIMIT` | 0 | Dzienny limit tokenów / kosztu w USD (0 = bez limitu) |
| `TOOLS_MODE` | `lazy` | `lazy` -- narzędzia na żądanie; `all` -- opisy wszystkich narzędzi przy każdym zapytaniu |
| `CONTEXT_KEEP_TURNS` | 2 | Wyniki narzędzi starsze niż tyle tur idą do modelu skrócone (0 = zawsze w całości) |
| `NOTIFY_LEVEL` | `important` | Kiedy powiadamiać: `all`, `important`, `critical` (zmiana w trakcie pracy: `/powiadomienia`) |
| `QUIET_HOURS` | -- | Cisza nocna, np. `22:00-07:00`: niekrytyczne wiadomości czekają do rana |
| `INSIGHTS_PER_DAY` | 3 | Najwyżej tyle podpowiedzi dziennie z powiadomieniem (reszta w skrzynce) |
| `INSIGHTS_INTERVAL` | 3600 | Co ile sekund szukać podpowiedzi (0 = wyłączone) |
| `AUTO_INVESTIGATE`, `AUTO_INVESTIGATE_DAILY` | `critical`, 4 | Pipe sam bada przyczynę alertu (`critical`, `all`, `off`) i limit badań dziennie |
| `WORKSPACE_DIR` | `/root/pipe-workspace` | Warsztat: katalog hosta na projekty i skrypty agenta |
| `WORKSPACE_AUTONOMY` | 1 | W warsztacie zapis plików i `project exec` bez pytania (0 = pytanie o każdą operację) |
| `PROJECT_MEMORY`, `PROJECT_CPUS` | `1g`, `1` | Limity kontenerów projektów (piaskownica i usługi) |
| `PROJECT_EXEC_TIMEOUT`, `PROJECT_BUILD_TIMEOUT` | 600, 1800 | Limit czasu (s) komendy w piaskownicy i budowania/uruchomienia |
| `PROJECT_MAX_MB` | 1024 | Największy rozmiar sklonowanego repozytorium |

## Zatrzymanie

```bash
docker-compose down          # tryb docker
systemctl stop pipe          # tryb native
```
