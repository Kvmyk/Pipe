# Discord Client — Coming Soon

Klient Discord dla VPS Management Agent.

> 🚧 **Status: Placeholder — Pull Requests mile widziane!**

## Jak zaimplementować nowy klient

Każdy klient implementuje ten sam protokół socket JSON:

### Protokół

Backend nasłuchuje na Unix socket `/tmp/vps-agent.sock`.

**Żądanie wiadomości:**
```json
{"message": "tekst użytkownika", "session_id": "uuid-per-uzytkownik", "interface": "discord:user_id"}
```

**Żądanie potwierdzenia:**
```json
{"confirm": true, "session_id": "uuid-per-uzytkownik"}
```

**Odpowiedź (JSON lines — jedna odpowiedź per linia):**
```json
{"response": "tekst odpowiedzi", "status": "ok|confirm|error", "done": false}
{"response": "", "status": "ok", "done": true}
```

Serwer wysyła wiele linii JSON (streaming). Ostatnia linia ma `"done": true`.

### Stany odpowiedzi

| Status | Znaczenie |
|--------|-----------|
| `ok` | Odpowiedź agenta |
| `confirm` | Agent czeka na potwierdzenie użytkownika |
| `error` | Błąd agenta |

### Implementacja w Pythonie

```python
import asyncio
import json

async def chat_with_agent(message: str, session_id: str) -> list[dict]:
    reader, writer = await asyncio.open_unix_connection("/tmp/vps-agent.sock")
    
    request = {"message": message, "session_id": session_id, "interface": "discord:123"}
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

### Wymagania strukturalne

- Jeden `session_id` per użytkownik (np. `str(discord_user_id)`)
- Wysyłaj `interface` jako `discord:user_id` dla identyfikacji w audit logu
- Obsłuż status `confirm` — zapytaj użytkownika o potwierdzenie i wyślij `{"confirm": true|false, ...}`
- Obsłuż długie odpowiedzi — Discord ma limit 2000 znaków per wiadomość

## Chcesz dodać Discord?

1. Fork tego repo
2. Utwórz `clients/discord/discord_bot.py`
3. Zaimplementuj protokół socket (patrz wyżej)
4. Dodaj `requirements.txt` i `README.md`
5. Otwórz Pull Request 🎉
