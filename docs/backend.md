# Backend -- PipeClaw

PipeClaw v0.3

Backend agenta VPS. Dziala bezposrednio na serwerze (Mikrus) i wystawia lokalny Unix socket dla klientow.

## Wymagania

- Docker 24+
- Docker Compose v2
- Linux (Unix socket nie dziala na Windows/macOS bez dodatkowej konfiguracji)

## Konfiguracja

### 1. Skopiuj przykladowy plik konfiguracyjny

```bash
cp .env.example .env
```

### 2. Ustaw klucz API

Otworz `.env` i uzupelnij `LLM_API_KEY`.

Darmowy klucz Gemini: https://aistudio.google.com/ -> "Get API key"

Domyslnie uzywamy `gemini-2.0-flash` -- szybki, darmowy model z limitem 15 RPM.

### `.env` w katalogu `backend/`

```env
# URL i klucz API zgodny ze standardem OpenAI
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-twoj-klucz
LLM_MODEL=gpt-4o

# Zabezpieczenie (Opcjonalne, ale bardzo zalecane)
# Jesli jest ustawione, chroni interfejs TCP przed nieautoryzowanym dostepem z serwera.
AGENT_TOKEN=twoj-tajny-token
```

Mozesz tez uzyc innych providerow (OpenAI-compatible):

```env
# OpenAI
LLM_BASE_URL=https://api.openai.com/v1/
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini

# Groq (bardzo szybki, darmowy)
LLM_BASE_URL=https://api.groq.com/openai/v1/
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.3-70b-versatile

# Anthropic (przez OpenAI-compatible endpoint)
LLM_BASE_URL=https://api.anthropic.com/v1/
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-3-5-haiku-latest
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
[VPS Agent] Model       : gemini-2.0-flash
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
