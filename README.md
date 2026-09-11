# PipeClaw

**v0.5.0** -- Autonomiczny agent AI do zarzadzania serwerem VPS. 
System zostal zaprojektowany z mysla o dzialaniu na wielu platformach -- mozesz komunikowac sie z serwerem uzywajac dedykowanego CLI, bezposrednio przez bota na Telegramie, a wkrotce takze przez Discorda dzieki ujednoliconemu protokolowi zadan.

---

## Szybki start

Szczegolowa instrukcja krok po kroku: [docs/quickstart.md](./docs/quickstart.md)

### 1. Postaw backend na serwerze

Wykonaj na serwerze (Mikrus):

```bash
git clone https://github.com/user/pipeclaw
cd pipeclaw
python3 -m backend.configure   # kreator: provider, klucz API, model, test
cd backend
docker-compose up -d
```

Kreator pobiera aktualna liste modeli prosto od providera i sprawdza, czy wybrany model obsluguje
tool calling. Nie wymaga instalowania zadnych pakietow. Szczegoly: [docs/backend.md](./docs/backend.md)

### 2. Podlacz CLI ze swojego laptopa

Na laptopie -- skrypt automatycznie doda skrot `pipeclaw` do Twojego terminala:

```powershell
# Windows
.\install.ps1
```

```bash
# Linux / macOS
bash install.sh
```

Od teraz w kazdym terminalu wpisujesz:

```
pipeclaw
```

CLI pyta o haslo SSH, laczy sie z agentem na serwerze i czeka na Twoje polecenia.

### 3. Postaw Telegram bota na serwerze

Telegram bot dziala na tym samym serwerze co backend (laczy sie przez Unix socket):

```bash
# Na serwerze
cd pipeclaw/clients/telegram
cp .env.example .env
# Uzupelnij TELEGRAM_BOT_TOKEN i TELEGRAM_ALLOWED_USER_IDS w .env
```

Bot uruchamia sie automatycznie razem z backendem przez `docker-compose up -d` w katalogu `backend/`.

---

## Architektura

| Komponent | Gdzie dziala | Polaczenie z backendem |
|-----------|--------------|------------------------|
| `backend/` | Serwer (Mikrus) | -- to jest backend |
| `clients/cli/` | Twoj laptop | SSH tunnel -> TCP `127.0.0.1:7379` |
| `clients/telegram/` | Serwer (Mikrus) | Unix socket `/tmp/vps-agent.sock` |
| `clients/discord/` | -- | Placeholder -- PR welcome |
| `clients/webui/` | -- | Placeholder -- PR welcome |

Komunikacja: prosty protokol JSON (linia po linii):
- Zadanie: `{"message": "tekst", "session_id": "uuid", "interface": "cli", "token": "..."}` (`token` tylko gdy `AGENT_TOKEN` jest ustawiony)
- Odpowiedz: `{"response": "tekst", "status": "ok|confirm|error", "done": true}`

---

## Narzedzia agenta

| Narzedzie | Opis |
|-----------|------|
| `execute_command` | Wykonywanie komend shell na serwerze |
| `read_file` / `write_file` | Odczyt i zapis plikow |
| `git_command` | Zarzadzanie repozytoriami Git (status, log, diff, commit, push) |
| `system_stats` | Szczegolowe statystyki systemowe (CPU, RAM, dysk, procesy) |
| `docker_manage` | Zarzadzanie kontenerami i obrazami Docker |
| `network_info` | Diagnostyka sieciowa (porty, polaczenia, ping, curl, DNS) |
| `cron_manage` | Zarzadzanie zadaniami cron |

---

## Providerzy LLM

Dowolny endpoint zgodny z OpenAI. Wbudowane presety: **Google Gemini** (domyslny, darmowy tier),
**OpenAI**, **Anthropic Claude**, **OpenRouter**, **Groq**, **DeepSeek**, **Mistral**, **xAI Grok**,
**Z.ai (GLM)**, **Moonshot Kimi**, **Together AI**, **Cerebras**, **Fireworks** i lokalna **Ollama**.

Wlasnego providera (vLLM, LM Studio, LiteLLM, proxy firmowe...) dodasz kreatorem -- opcja **Inny**.
Lista modeli nie jest wpisana na sztywno: kreator i backend pobieraja ja na zywo z API providera,
a przy starcie backend ostrzega, jesli skonfigurowany model zostal wycofany.

---

## Moduly backendu

| Modul | Opis |
|-------|------|
| `core/agent.py` | Petla LLM z tool calling, max 10 iteracji |
| `core/handlers/` | Implementacje narzedzi -- po jednym module na obszar |
| `core/session.py` | Sesja uzytkownika (historia, `cwd`, oczekujace potwierdzenie) |
| `core/executor.py` | `execute` / `read_file` / `write_file` -- subprocess z 30s timeoutem |
| `core/security.py` | Klasyfikacja komend: `safe` / `confirm` / `forbidden` |
| `core/audit.py` | Append-only audit log |
| `core/tools.py` | Definicje narzedzi OpenAI function calling |
| `config/settings.py` | Konfiguracja z `.env` |
| `config/providers.py` | Presety providerow LLM, wlasni providerzy, filtrowanie list modeli |
| `configure.py` | Kreator konfiguracji providera (`python3 -m backend.configure`) |
| `config/prompts.py` | System prompt (niemodyfikowalny) |

---

## Bezpieczenstwo

System operuje w oparciu o 3-poziomowa klase bezpieczenstwa:
- **SAFE** -- komendy read-only (np. `ls`, `df`, `docker ps`) wykonywane bez pytania
- **CONFIRM** -- komendy modyfikujace (np. `rm`, `git commit`) -- agent wymaga zatwierdzenia przez GUI
- **FORBIDDEN** -- komendy destruktywne (np. `rm -rf /`, `mkfs`) odrzucane bezwzglednie

**Wazne:** Do pliku `.env` w katalogu `backend/` warto dodac `AGENT_TOKEN=tajny-ciag-znakow`. Wtedy kazde zadanie -- lacznie z potwierdzeniem operacji -- musi zawierac ten token; ten sam ciag podaj CLI (`--token` lub zmienna `AGENT_TOKEN`) oraz botowi Telegrama (`AGENT_TOKEN` w `clients/telegram/.env`). Jesli tego nie zrobisz, agent dziala na pelnym zaufaniu na interfejsie lokalnym (zabezpieczonym tylko przez SSH).

- Agent dziala jako dedykowany user bez sudo (`vpsagent`)
- Kazda operacja zapisywana do audit logu
- Zawartosc pliku nigdy nie trafia do audit logu (moze zawierac sekrety)
- Lista bezwzglednie zakazanych wzorcow (`rm -rf /`, fork bomb, `curl | bash`, itp.)

---

## Dokumentacja

- [Szybki start](./docs/quickstart.md)
- [Backend](./docs/backend.md)
- [CLI](./docs/cli.md)
- [Telegram](./docs/telegram.md)
- [Bezpieczenstwo](./docs/security.md)
- [Protokol komunikacji](./docs/protocol.md)
- [Historia zmian](./docs/changelog.md)
