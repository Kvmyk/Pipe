# Protokol komunikacji -- Pipe

Pipe v0.8.0

## Opis

Backend nassluchuje rownoczesnie na:
1. Unix socket (`/tmp/vps-agent.sock`) -- dla klientow na tym samym serwerze (Telegram bot)
2. TCP `127.0.0.1:7379` -- dla SSH tunnel z laptopa (CLI)

Komunikacja odbywa sie przez prosty protokol JSON lines (jedna linia JSON = jedna wiadomosc).

## Format wiadomosci

### Zadanie -- wiadomosc uzytkownika

```json
{
  "message": "tekst wiadomosci",
  "session_id": "uuid-per-uzytkownik",
  "interface": "cli|telegram:user_id|discord:user_id",
  "token": "tajny-token"
}
```

### Zadanie -- potwierdzenie operacji

```json
{
  "confirm": true,
  "session_id": "uuid-per-uzytkownik",
  "token": "tajny-token"
}
```

### Zadanie -- komenda klienta

Komendy `/server`, `/skille` i skille uruchamiane wlasna komenda (Telegram, CLI) wysylaja:

```json
{
  "command": "run_skill",
  "session_id": "uuid-per-uzytkownik",
  "interface": "cli",
  "name": "odnow_certyfikat",
  "args": "tylko dla example.com",
  "token": "tajny-token"
}
```

| `command` | Dodatkowe pola | Odpowiedz |
|---|---|---|
| `list_skills` | -- | jedna linia z `"data": {"skills": [{"name", "description", "command"}]}` |
| `server_md` | -- | jedna linia z `"data": {"content": "..."}` (pusty, gdy SERVER.md nie istnieje) |
| `scan_server` | -- | streaming jak zwykla wiadomosc: agent bada serwer i tworzy albo aktualizuje SERVER.md |
| `run_skill` | `name` (nazwa albo komenda skilla), opcjonalnie `args` | streaming jak zwykla wiadomosc |

Tresc wiadomosci dla agenta (`scan_server`, `run_skill`) buduje backend -- klient wysyla tylko komende.
Pole `command` skilla to nazwa zgodna z zasadami Telegrama (male litery, cyfry, `_`, do 32 znakow). Jest puste,
gdy nazwa skilla jest zarezerwowana (`status`, `server`, `skille`, ...) albo po skroceniu koliduje z innym skillem.

### Odpowiedz

```json
{
  "response": "tekst odpowiedzi",
  "status": "ok|confirm|error",
  "done": false
}
```

Serwer wysyla wiele linii JSON (streaming). Ostatnia linia ma `"done": true`.

### Autoryzacja tokenem

Pole `token` jest wymagane tylko wtedy, gdy w `backend/.env` ustawiono `AGENT_TOKEN`.
Gdy `AGENT_TOKEN` jest pusty, pole mozna pominac i backend przyjmuje kazde zadanie.

Token jest sprawdzany dla **kazdego** typu zadania -- rowniez dla potwierdzen
(`confirm`), zeby nieuwierzytelniony klient nie mogl zatwierdzic operacji
oczekujacej w cudzej sesji. Przy blednym tokenie backend odpowiada:

```json
{"response": "Blad: Nieprawidlowy token autoryzacji.", "status": "error", "done": true}
```

## Stany odpowiedzi

| Status | Znaczenie |
|--------|-----------|
| `ok` | Standardowa odpowiedz agenta |
| `confirm` | Agent czeka na potwierdzenie uzytkownika |
| `error` | Blad agenta lub odmowa |

## Przebieg sesji

1. Klient nawiazuje polaczenie (Unix socket lub TCP)
2. Klient wysyla zadanie (linia JSON + `\n`)
3. Serwer odpowiada jedna lub wieloma liniami JSON
4. Ostatnia linia ma `"done": true`
5. Jesli `"status": "confirm"` -- klient pyta uzytkownika o potwierdzenie
6. Klient wysyla `{"confirm": true|false, "session_id": "..."}` 
7. Serwer kontynuuje przetwarzanie i wysyla kolejne odpowiedzi

## Implementacja w Pythonie

```python
import asyncio
import json

async def chat_with_agent(message: str, session_id: str) -> list[dict]:
    reader, writer = await asyncio.open_unix_connection("/tmp/vps-agent.sock")
    
    request = {"message": message, "session_id": session_id, "interface": "my-client"}
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    
    responses = []
    while True:
        raw = await reader.readline()
        resp = json.loads(raw)
        responses.append(resp)
        if resp.get("done"):
            break
    
    writer.close()
    return responses
```

## Wymagania dla nowych klientow

- Jeden `session_id` per uzytkownik (np. `str(user_id)`)
- Wysylaj `interface` dla identyfikacji w audit logu
- Obsluz status `confirm` -- zapytaj uzytkownika i wyslij odpowiedz
- Obsluz dlugie odpowiedzi -- podziel na mniejsze wiadomosci jesli platforma ma limit
