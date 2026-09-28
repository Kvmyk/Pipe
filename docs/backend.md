# Backend -- Pipe

Pipe v0.9.0

Backend agenta. Dziala na serwerze (Docker, natywnie z systemd albo w Kubernetesie -- [deploy.md](./deploy.md))
i wystawia lokalny Unix socket i port TCP 127.0.0.1:7379 dla klientow.

## Wymagania

- Linux, Python 3.11+
- tryb docker: Docker 24+ z Compose v2 (instalator doinstaluje je z repozytorium dystrybucji)
- tryb native: systemd; tryb kubernetes: klaster z PVC

## Moduly

| Modul | Rola |
|-------|------|
| `server.py` | Unix socket + TCP, ramki JSON lines, komendy klientow, subskrypcja zdarzen, start czuwania |
| `core/agent.py` | Petla LLM z tool calling (`AGENT_MAX_ITERATIONS`), potwierdzenia, redakcja i przycinanie wynikow, historia |
| `core/handlers/` | Jeden modul na obszar narzedzi; `handle_<nazwa>` = narzedzie `<nazwa>` |
| `core/security.py` | Klasyfikator komend (fail-closed, tokenowy, swiadomy cudzyslowow), workspace |
| `core/runtime.py` | Tryb dzialania (docker / native / kubernetes) i mapowanie sciezek hosta |
| `core/hostinfo.py` | Stan hosta z `/proc`: RAM, CPU, dyski, gniazda -- bez uruchamiania komend |
| `core/infra.py` | Odkrywanie infrastruktury (Docker, nginx, Caddy, Traefik, systemd, git, Kubernetes) i mapa Mermaid |
| `core/diagram.py` | Mermaid -> PNG + ASCII (mermaidx, offline) |
| `core/targets.py` | Zdalne cele: rejestr, walidacja, budowanie komend ssh / docker exec / kubectl |
| `core/workers.py` | Workery: pod-agenci na petli agenta, tylko odczyty, rownolegle |
| `core/routines.py` | Rutyny: harmonogram cron, rejestr |
| `core/watch.py` | Czuwanie: sprawdzenia, alerty, powiadomienia (pub/sub), uruchamianie rutyn |
| `core/memory.py` | SERVER.md, DIRECTORY, skille, VIBE, wykrywanie i redakcja sekretow |
| `core/vibe.py` | Nauka stylu rozmowy w tle |
| `core/events.py` | Zdarzenia strumienia: tekst, `Attachment`, `Progress` |
| `core/executor.py` | `execute` (limit czasu, zabijanie grupy procesow), `read_file`, `write_file` |
| `core/audit.py` | Append-only audit log |
| `core/tools.py` | Schematy narzedzi (OpenAI function calling) |
| `config/` | `settings.py` (zmienne `.env`), `providers.py` (presety LLM), `prompts.py` |
| `configure.py` | Kreator providera (`--check`, `--models`, `--providers`, `--from-env`) |

## Konfiguracja

### Skonfiguruj providera LLM

Z katalogu glownego repozytorium uruchom kreator (sam utworzy `backend/.env` z `.env.example`):

```bash
python3 -m backend.configure
```

Kreator:
1. pokaze liste providerow i zapyta o klucz API (z linkiem, gdzie go zdobyc),
2. pobierze **aktualna** liste modeli prosto z API providera -- nowe modele sa widoczne od razu, bez aktualizacji Pipe,
3. sprawdzi na zywo, czy wybrany model obsluguje tool calling (bez tego agent nie wykona zadnej komendy),
4. zapisze `backend/.env` (utworzy go z `.env.example`, jesli nie istnieje; uprawnienia `600`).

Kreator uzywa wylacznie biblioteki standardowej Pythona -- nie trzeba niczego instalowac.

Przydatne tryby:

```bash
python3 -m backend.configure --check      # test obecnej konfiguracji (lista modeli + tool calling)
python3 -m backend.configure --models     # aktualne modele obecnego providera
python3 -m backend.configure --providers  # wszyscy dostepni providerzy
```

### Wbudowani providerzy

Wszyscy przez API zgodne z OpenAI Chat Completions. Adresy zweryfikowane we wrzesniu 2026.

| `LLM_PROVIDER` | Provider | Domyslny model | Uwagi |
|---|---|---|---|
| `gemini` | Google Gemini | `gemini-3.8-flash` | Domyslny. Darmowy tier dla modeli Flash |
| `openai` | OpenAI | `gpt-5.6-terra` | GPT-6 wymaga Responses API do tool callingu -- uzyj GPT-5.6 |
| `anthropic` | Anthropic Claude | `claude-sonnet-5` | Przez warstwe zgodnosci z OpenAI SDK |
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
| `ollama` | Ollama (lokalnie) | -- (wybierz z listy) | Bez klucza, patrz nizej |

Domyslny model to tylko punkt startowy. Przy starcie backend sprawdza, czy skonfigurowany model nadal
jest na liscie providera, i wypisuje ostrzezenie z propozycjami, jesli zostal wycofany.

### Reczna konfiguracja `.env`

Zamiast kreatora mozesz edytowac `backend/.env` recznie:

```env
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_MODEL=            # pusty = domyslny model providera
```

Zamiast `LLM_API_KEY` mozesz ustawic zmienna specyficzna dla providera, np. `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `ANTHROPIC_API_KEY` -- `LLM_API_KEY` ma pierwszenstwo.

Opcjonalnie:

```env
LLM_BASE_URL=https://proxy.example.com/v1   # nadpisuje adres z presetu (np. proxy firmowe)
LLM_REASONING_EFFORT=low                     # dla modeli, ktore obsluguja ten parametr
LLM_TIMEOUT=120                              # limit czasu odpowiedzi LLM w sekundach
```

Stare pliki `.env` (tylko `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`, bez `LLM_PROVIDER`) dzialaja
dalej -- provider jest rozpoznawany po adresie.

### Wlasny provider

Dowolny endpoint zgodny z OpenAI (vLLM, LM Studio, LiteLLM, firmowe proxy...) dodasz kreatorem --
wybierz opcje **Inny** i podaj adres. Provider zostanie zapisany w `backend/data/providers.json`
i od tej pory jest dostepny jak wbudowany (`LLM_PROVIDER=<id>`).

Mozesz tez edytowac ten plik recznie:

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

Wpis o `id` wbudowanego providera nadpisuje go -- tak poprawisz nieaktualny adres bez czekania na nowa
wersje Pipe. Katalog `backend/data/` jest zamontowany w kontenerze, wiec zmiany nie wymagaja przebudowy
obrazu -- wystarczy `docker-compose restart vps-agent`.

### Ollama (modele lokalne)

Kontener widzi hosta pod adresem `host.docker.internal` (ustawione w `docker-compose.yml`). Ollama domyslnie
nasluchuje tylko na `127.0.0.1`, wiec musisz ja uruchomic z `OLLAMA_HOST=0.0.0.0`, zeby kontener ja widzial.
Model musi obslugiwac tool calling -- kreator to sprawdzi.

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

## Pamiec agenta: SERVER.md i skille

Agent ma kilka rodzajow trwalej pamieci: SERVER.md, skille (ponizej), a takze DIRECTORY (mapa repozytoriow
i katalogow) i VIBE (styl rozmowy) -- te dwie opisuje [features.md](./features.md). Wszystko lezy na hoscie
w `backend/data/` (w kontenerze `/app/data`, zmienna `DATA_DIR`), przetrwa restart i przebudowe obrazu
i nie trafia do gita. Tam sa tez `targets.json`, `routines.json`, `watch_state.json` i audit log.

### SERVER.md

Notatki agenta o serwerze -- jak `AGENTS.md`, ale dla serwera. Agent aktualizuje je sam (narzedzie `server_md`),
gdy pozna trwaly fakt: uslugi, kontenery, domeny, porty, wazne sciezki, Twoje decyzje. Caly plik jest dolaczany
do system promptu kazdej rozmowy (do 12 000 znakow; sam plik moze miec do 20 000).

Na start mozesz poprosic: *"zbadaj serwer i utworz SERVER.md"*. Plik mozesz tez edytowac recznie:
`backend/data/SERVER.md`.

### Skille

Zapisane procedury wielokrotnego uzytku, w formacie Agent Skills:

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

W system prompcie jest tylko lista skilli (nazwa i opis). Pelna tresc agent wczytuje narzedziem `skill_manage`,
gdy zadanie pasuje do opisu -- dzieki temu wiele skilli nie zapycha kontekstu. Agent tworzy skill po wykonaniu
wieloetapowej procedury, ktora sie powtorzy, albo na Twoja prosbe.

Kazdy skill ma tez wlasna komende w Telegramie i CLI: myslniki zamieniaja sie na `_`, a nazwa jest skracana
do 32 znakow (limit Telegrama), np. `odnow-certyfikat` -> `/odnow_certyfikat`. Skill o nazwie zajetej przez
komende wbudowana (`status`, `server`, `skille`, ...) nie dostaje komendy -- uruchomisz go przez `/skille`
albo zwyklym tekstem.

### Bezpieczenstwo pamieci

- Zapis do pamieci nie wymaga potwierdzenia -- nie zmienia serwera, tylko notatki agenta. Kazdy zapis trafia do
  audit logu (sama sciezka, bez tresci).
- Skille i SERVER.md nie omijaja zasad bezpieczenstwa: kazda komenda z nich nadal przechodzi klasyfikacje
  i wymog potwierdzenia.
- SERVER.md jest wysylany do providera LLM z kazdym zapytaniem, dlatego zapis oczywistych sekretow (klucze
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

Powinienes zobaczyc:
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

## Narzedzia agenta

| Narzedzie | Opis | Wymaga potwierdzenia |
|-----------|------|----------------------|
| `execute_command` | Komendy shell | Zalezne od klasyfikacji |
| `read_file` | Odczyt plikow (sekrety redagowane) | Nie |
| `write_file` | Zapis plikow | Zawsze |
| `change_directory` | Katalog roboczy | Nie |
| `git_command` | Operacje Git | Modyfikujace: tak |
| `system_stats` | CPU/RAM/dyski/procesy z `/proc` hosta | Nie |
| `docker_manage` | Kontenery, obrazy, compose | Modyfikujace: tak |
| `network_info` | Porty hosta, polaczenia, ping, curl, DNS | Nie |
| `cron_manage` | Cron hosta (edycja tylko w trybie native) | Modyfikujace: tak |
| `diagram` | Mapa infrastruktury / wlasny diagram Mermaid jako obraz | Nie |
| `target_manage` | Rejestr zdalnych celow | Dodanie: tak |
| `remote_exec` | Komenda na zdalnym celu | Zalezne od klasyfikacji |
| `delegate` | Workery (tylko odczyty) | Nie -- zmiany wracaja jako propozycje |
| `routine_manage` | Rutyny wedlug harmonogramu | Dodanie: tak |
| `server_md`, `directory`, `skill_manage`, `vibe` | Pamiec agenta | Nie |

## Dodatkowe ustawienia `.env`

| Zmienna | Domyslnie | Opis |
|---------|-----------|------|
| `PIPE_RUNTIME` | `auto` | `docker` / `native` / `kubernetes` |
| `HOST_ROOT`, `HOST_PROC` | wg trybu | Nadpisanie sciezek hosta |
| `AGENT_MAX_ITERATIONS` | 15 | Limit krokow petli na jedna wiadomosc |
| `CONFIRMED_COMMAND_TIMEOUT` | 900 | Limit czasu (s) komend zatwierdzonych przez uzytkownika; odczyty maja 30 s |
| `REDACT_SECRETS` | 1 | Redakcja sekretow w wynikach narzedzi |
| `WORKER_MODEL` | model agenta | Model workerow i nauki VIBE |
| `WORKER_MAX_ITERATIONS`, `WORKER_TIMEOUT`, `MAX_WORKERS` | 8, 240, 6 | Limity workerow |
| `VIBE_EVERY` | 6 | Co ile wiadomosci odswiezac VIBE (0 = wylaczone) |
| `WATCH_ENABLED`, `WATCH_INTERVAL` | 1, 120 | Czuwanie |
| `WATCH_DISK_PCT`, `WATCH_MEM_PCT`, `WATCH_LOAD_FACTOR` | 90, 92, 2 | Progi alertow |

## Zatrzymanie

```bash
docker-compose down          # tryb docker
systemctl stop pipe          # tryb native
```
