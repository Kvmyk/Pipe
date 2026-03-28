# Telegram Bot — VPS Management Agent

Bot Telegram do zarządzania serwerem VPS przez agenta AI.

## Co to jest?

Bot Telegram to klient agenta. Łączy się z backendem przez Unix socket i przekazuje wiadomości z Telegrama do agenta. Odpowiedzi są formatowane w MarkdownV2 i wysyłane z powrotem do użytkownika.

Obsługuje potwierdzenia przez przyciski inline (✅ TAK / ❌ NIE).

## Wymagania

- Python 3.11+
- Działający backend (`backend/server.py` lub `docker-compose up -d` w `backend/`)
- Dostęp do Unix socket `/tmp/vps-agent.sock` (bot musi działać na tym samym serwerze co backend, lub mieć dostęp do socketa)
- Token bota Telegram (z [@BotFather](https://t.me/botfather))

## Konfiguracja

### 1. Utwórz bota przez @BotFather

```
/newbot
```

Skopiuj token bota.

### 2. Znajdź swoje user ID

Wyślij `/start` do [@userinfobot](https://t.me/userinfobot) — zwróci Twoje ID numeryczne.

### 3. Uzupełnij .env

```bash
cp .env.example .env
```

Edytuj `.env`:
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCxyz...
TELEGRAM_ALLOWED_USER_IDS=123456789
AGENT_SOCKET=/tmp/vps-agent.sock
```

Możesz dodać wiele ID oddzielonych przecinkami: `123456789,987654321`

## Uruchomienie

```bash
pip install -r requirements.txt
python telegram.py
```

## Dostępne komendy

| Komenda | Opis |
|---------|------|
| `/start` | Przywitanie i automatyczny status serwera |
| `/historia` | Ostatnie 10 wpisów z audit logu |

Możesz też pisać bezpośrednio, np.:
- _"ile mam wolnego miejsca na dysku?"_
- _"pokaż ostatnie błędy nginx"_
- _"zrestartuj docker compose"_

## Bezpieczeństwo

Bot **milczy** dla użytkowników spoza whitelisty — nie odpowiada żadną wiadomością. Dzięki temu nie ujawnia swojego istnienia nieautoryzowanym osobom.
