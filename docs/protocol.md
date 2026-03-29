# Protokol komunikacji -- Pipe

Pipe v0.1

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
  "interface": "cli|telegram:user_id|discord:user_id"
}
```

### Zadanie -- potwierdzenie operacji

```json
{
  "confirm": true,
  "session_id": "uuid-per-uzytkownik"
}
```

### Odpowiedz

```json
{
  "response": "tekst odpowiedzi",
  "status": "ok|confirm|error",
  "done": false
}
```

Serwer wysyla wiele linii JSON (streaming). Ostatnia linia ma `"done": true`.

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
