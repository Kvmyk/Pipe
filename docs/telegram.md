# Telegram Bot -- PipeClaw

PipeClaw v0.5.1

Bot Telegram do zarzadzania serwerem VPS przez agenta AI.

## Opis

Bot Telegram to klient agenta. Laczy sie z backendem przez Unix socket i przekazuje wiadomosci z Telegrama do agenta. Odpowiedzi sa formatowane w HTML i wysylane z powrotem do uzytkownika.

Obsluguje potwierdzenia przez przyciski inline (TAK / NIE).

## Formatowanie odpowiedzi

Bot uzywa trybu HTML Telegrama (nie MarkdownV2). Powod: MarkdownV2 wymaga escapowania 18 znakow specjalnych, co jest ekstremalnie podatne na bledy przy dynamicznej tresci (np. nazwy kontenerow z podkreslnikami). HTML wymaga escapowania tylko 3 znakow (<, >, &), co jest znacznie prostsze.

Obslugiwane tagi HTML:
- `<b>pogrubienie</b>` -- dla kluczowych danych
- `<i>kursywa</i>` -- dla nazw plikow
- `<code>kod inline</code>` -- dla wartosci i polecen
- `<pre>blok kodu</pre>` -- dla outputu komend
- `<u>podkreslenie</u>` -- jesli potrzebne

Bot automatycznie konwertuje resztki Markdowna (jesli LLM je wygeneruje) na odpowiadajace tagi HTML.

Tekst w backtickach -- w tym komendy w potwierdzeniach -- jest pokazywany doslownie, bez konwersji Markdowna. Przed kliknieciem TAK widzisz wiec dokladnie te komende, ktora zostanie wykonana (lacznie z `*`, `_`, `\\`, `<`, `&` i backtickami). Znaki `<`, `>` i `&` poza tagami sa escapowane automatycznie, wiec Telegram nie odrzuca wiadomosci. Kod formatowania: `clients/telegram/tg_format.py`.

## Wymagania

- Python 3.11+
- Dzialajacy backend (`docker-compose up -d` w `backend/`)
- Dostep do Unix socket `/tmp/vps-agent.sock`
- Token bota Telegram (z @BotFather: https://t.me/botfather)

## Konfiguracja

### 1. Utworz bota przez @BotFather

```
/newbot
```

Skopiuj token bota.

### 2. Znajdz swoje user ID

Wyslij `/start` do @userinfobot (https://t.me/userinfobot) -- zwroci Twoje ID numeryczne.

### 3. Uzupelnij .env

```bash
cp .env.example .env
```

Edytuj `.env`:
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCxyz...
TELEGRAM_ALLOWED_USER_IDS=123456789
AGENT_SOCKET=/tmp/vps-agent.sock

# Musi byc identyczny z AGENT_TOKEN w backend/.env.
# Zostaw pusty, jesli backend nie wymaga tokenu.
AGENT_TOKEN=
```

Mozesz dodac wiele ID oddzielonych przecinkami: `123456789,987654321`

## Uruchomienie

Bot uruchamia sie automatycznie razem z backendem przez `docker-compose up -d`.

Reczne uruchomienie:
```bash
pip install -r requirements.txt
python bot.py
```

## Dostepne komendy

| Komenda | Opis |
|---------|------|
| `/start` | Przywitanie |
| `/status` | Status serwera (CPU, RAM, dysk) |
| `/historia` | Ostatnie 10 wpisow z audit logu |

Mozesz tez pisac bezposrednio, np.:
- "ile mam wolnego miejsca na dysku?"
- "pokaz ostatnie bledy nginx"
- "zrestartuj docker compose"
- "pokaz status git w /home/user/projekt"
- "jakie porty sa otwarte?"

## Bezpieczenstwo

Bot milczy dla uzytkownikow spoza whitelisty -- nie odpowiada zadna wiadomoscia. Dzieki temu nie ujawnia swojego istnienia nieautoryzowanym osobom.
