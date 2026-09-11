# Backend -- PipeClaw

PipeClaw v0.5.0

Backend agenta VPS. Dziala bezposrednio na serwerze (Mikrus) i wystawia lokalny Unix socket dla klientow.

## Wymagania

- Docker 24+
- Docker Compose v2
- Linux (Unix socket nie dziala na Windows/macOS bez dodatkowej konfiguracji)

## Konfiguracja

### Skonfiguruj providera LLM

Z katalogu glownego repozytorium uruchom kreator (sam utworzy `backend/.env` z `.env.example`):

```bash
python3 -m backend.configure
```

Kreator:
1. pokaze liste providerow i zapyta o klucz API (z linkiem, gdzie go zdobyc),
2. pobierze **aktualna** liste modeli prosto z API providera -- nowe modele sa widoczne od razu, bez aktualizacji PipeClaw,
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
wersje PipeClaw. Katalog `backend/data/` jest zamontowany w kontenerze, wiec zmiany nie wymagaja przebudowy
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
[VPS Agent] Serwer gotowy. Ctrl+C aby zatrzymac.
```

## Narzedzia agenta

Backend udostepnia agentowi nastepujace narzedzia:

| Narzedzie | Opis | Wymaga potwierdzenia |
|-----------|------|----------------------|
| `execute_command` | Wykonywanie komend shell | Zalezne od klasyfikacji |
| `read_file` | Odczyt plikow | Nie |
| `write_file` | Zapis plikow | Zawsze |
| `git_command` | Operacje Git | Modyfikujace: tak |
| `system_stats` | Statystyki CPU/RAM/dysk | Nie |
| `docker_manage` | Zarzadzanie kontenerami | Modyfikujace: tak |
| `network_info` | Diagnostyka sieciowa | Nie |
| `cron_manage` | Zadania cron | Modyfikujace: tak |

## Tworzenie dedykowanego uzytkownika (zalecane)

Agent powinien dzialac jako dedykowany uzytkownik bez uprawnien roota:

```bash
# Utworz uzytkownika
sudo useradd --system --no-create-home --shell /bin/false vpsagent

# Ustaw uprawnienia sudoers (tylko konkretne komendy)
sudo visudo -f /etc/sudoers.d/vpsagent
```

Zawartosc `/etc/sudoers.d/vpsagent`:
```
vpsagent ALL=(ALL) NOPASSWD: /bin/systemctl, /usr/bin/journalctl, /usr/bin/apt, /usr/bin/docker, /usr/local/bin/docker-compose
```

## Zatrzymanie

```bash
docker-compose down
```
