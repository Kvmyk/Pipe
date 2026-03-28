# Backend — VPS Management Agent

Backend agenta VPS. Działa **bezpośrednio na serwerze (Mikrus)** i wystawia lokalny Unix socket dla klientów.

## Wymagania

- Docker 24+
- Docker Compose v2
- Linux (Unix socket nie działa na Windows/macOS bez dodatkowej konfiguracji)

## Konfiguracja

### 1. Skopiuj przykładowy plik konfiguracyjny

```bash
cp .env.example .env
```

### 2. Ustaw klucz API

Otwórz `.env` i uzupełnij `LLM_API_KEY`.

**Darmowy klucz Gemini:** https://aistudio.google.com/ → "Get API key"

Domyślnie używamy `gemini-2.0-flash` — szybki, darmowy model z limitem 15 RPM.

Możesz też użyć innych providerów (OpenAI-compatible):

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

Powinieneś zobaczyć:
```
[VPS Agent] Uruchamiam serwer na /tmp/vps-agent.sock
[VPS Agent] Model: gemini-2.0-flash
[VPS Agent] Gotowy. Ctrl+C aby zatrzymać.
```

## Tworzenie dedykowanego użytkownika (zalecane)

Agent powinien działać jako dedykowany użytkownik bez uprawnień roota:

```bash
# Utwórz użytkownika
sudo useradd --system --no-create-home --shell /bin/false vpsagent

# Ustaw uprawnienia sudoers (tylko konkretne komendy)
sudo visudo -f /etc/sudoers.d/vpsagent
```

Zawartość `/etc/sudoers.d/vpsagent`:
```
vpsagent ALL=(ALL) NOPASSWD: /bin/systemctl, /usr/bin/journalctl, /usr/bin/apt, /usr/bin/docker, /usr/local/bin/docker-compose
```

## Bezpieczeństwo

**Dlaczego nie root?**
Agent może wykonywać komendy systemowe. Jeśli LLM zostanie oszukany (prompt injection), ograniczone uprawnienia minimalizują szkody.

**Jak działa allowlist?**
Komendy są klasyfikowane w trzech krokach:
1. `FORBIDDEN` — odmów bezwzględnie (np. `rm -rf /`, fork bomb)
2. `CONFIRM` — zapytaj użytkownika o potwierdzenie (np. edycja `/etc/`, reboot)
3. `SAFE` — wykonaj od razu (np. `df`, `systemctl status`, `docker ps`)

**Audit log:**
Każda operacja (łącznie z odmowami) zapisywana do `~/.vps_agent_audit.log`.
Zawartość pliku **nigdy** nie trafia do logu — tylko ścieżka i timestamp.

## Zatrzymanie

```bash
docker-compose down
```
