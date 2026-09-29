# Protokol komunikacji -- Pipe

Pipe v0.9.2

## Opis

Backend nassluchuje rownoczesnie na:
1. Unix socket (`/tmp/vps-agent.sock`) -- dla klientow na tym samym serwerze (Telegram bot)
2. TCP `127.0.0.1:7379` -- dla SSH tunnel z laptopa (CLI)

Komunikacja odbywa sie przez prosty protokol JSON lines (jedna linia JSON = jedna wiadomosc).
W trybie Kubernetes dostep do portu TCP daje `kubectl port-forward svc/pipe 7379:7379`.

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

Komendy "/" klientow (Telegram, CLI) wysylaja `{"command": ...}`:

```json
{
  "command": "run_skill",
  "session_id": "uuid-per-uzytkownik",
  "interface": "cli:kuba",
  "name": "odnow_certyfikat",
  "args": "tylko dla example.com",
  "token": "tajny-token"
}
```

| `command` | Dodatkowe pola | Odpowiedz |
|---|---|---|
| `list_skills` | -- | jedna linia z `"data": {"skills": [{"name", "description", "command"}]}` |
| `server_md` | -- | `"data": {"content": "..."}` (pusty, gdy SERVER.md nie istnieje) |
| `scan_server` | -- | streaming: agent bada serwer, tworzy/aktualizuje SERVER.md i DIRECTORY |
| `status` | -- | streaming: podsumowanie stanu serwera |
| `run_skill` | `name` (nazwa albo komenda skilla), opcjonalnie `args` | streaming |
| `diagram` | opcjonalnie `args` (tytul) | mapa infrastruktury **bez LLM**: ramka z `attachment`, potem `done` z `"data": {"summary": "..."}` |
| `directory` | -- | `"data": {"entries": [{"path", "kind", "description", "remote", "branch", ...}], "text": "..."}` |
| `vibe` | `args`: puste albo `reset` | `"data": {"content": "..."}` albo `{"content": "", "reset": true\|false}` (VIBE uzytkownika z `interface`) |
| `alerts` | -- | `"data": {"active": [alert], "recent": [zdarzenie], "enabled": bool}` |
| `targets` | -- | `"data": {"targets": ["opis celu", ...]}` |
| `routines` | -- | `"data": {"routines": ["opis rutyny", ...]}` |
| `history` | -- | `"data": {"entries": ["linia audit logu", ...]}` (ostatnie 15) |
| `investigate` | `id` alertu | streaming: agent bada alert czuwania |
| `subscribe` | -- | polaczenie zostaje otwarte; zdarzenia czuwania (ponizej) do rozlaczenia klienta |

Tresc wiadomosci dla agenta (`scan_server`, `status`, `run_skill`, `investigate`) buduje backend -- klient
wysyla tylko komende. Wiadomosci zbudowane przez backend nie ucza VIBE.
Pole `command` skilla to nazwa zgodna z zasadami Telegrama (male litery, cyfry, `_`, do 32 znakow). Jest puste,
gdy nazwa skilla jest zarezerwowana (`status`, `mapa`, `server`, `skille`, `katalogi`, `vibe`, `alerty`,
`cele`, `rutyny`, ...) albo po skroceniu koliduje z innym skillem.

### Odpowiedz

```json
{
  "response": "tekst odpowiedzi",
  "status": "ok|confirm|error",
  "done": false
}
```

Serwer wysyla wiele linii JSON (streaming). Ostatnia linia ma `"done": true`.
Ramka moze zawierac dodatkowo (wtedy `response` jest pusty, wiec starsi klienci ja pomijaja):

**Zalacznik** -- plik dla uzytkownika, np. diagram:

```json
{"response": "", "status": "ok", "done": false, "attachment": {
  "name": "mapa-infrastruktury.png", "mime": "image/png", "caption": "Mapa infrastruktury",
  "data": "<base64>", "source": "<kod Mermaid>", "text": "<podglad ASCII albo pusty>"}}
```

**Postep** -- status posredni (np. co robi worker), do pokazania na biezaco, nie wchodzi do odpowiedzi:

```json
{"response": "", "status": "ok", "done": false, "event": {"type": "progress", "text": "web-1 $ uptime", "source": "worker:web-1"}}
```

Ramki z diagramami maja setki KB -- klient musi czytac linie z wiekszym limitem niz domyslne 64 KiB
asyncio (`open_connection(..., limit=32 * 1024 * 1024)`).

### Zdarzenia czuwania (`subscribe`)

Po `{"command": "subscribe"}` serwer wysyla `{"event": {"type": "subscribed"}}`, a potem, bez `done`:

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "alert", "id": "3f2a9c1d0b", "key": "disk:/", "severity": "warning|critical",
  "title": "Dysk / zapelniony w 93%", "detail": "wolne 3.1 GB z 40 GB (/dev/sda1)",
  "since": "2026-09-24 14:02", "state": "new|resolved|event", "at": "2026-09-24 14:02"}}
```

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "routine", "name": "poranny-przeglad", "status": "OK|PROBLEM", "report": "...", "at": "..."}}
```

`state`: `new` -- nowy albo eskalowany alert, `resolved` -- problem minal, `event` -- zdarzenie jednorazowe
(nowy publiczny port, zatrzymany kontener). `investigate` z `id` prosi agenta o zbadanie alertu.

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
    reader, writer = await asyncio.open_unix_connection("/tmp/vps-agent.sock", limit=32 * 1024 * 1024)
    
    request = {"message": message, "session_id": session_id, "interface": "my-client"}
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    
    responses = []
    while True:
        raw = await reader.readline()
        resp = json.loads(raw)
        if resp.get("attachment"):
            save(resp["attachment"])            # np. base64 -> plik PNG
        elif resp.get("event"):
            show_progress(resp["event"])
        else:
            responses.append(resp)
        if resp.get("done"):
            break
    
    writer.close()
    return responses
```

## Wymagania dla nowych klientow

- Jeden `session_id` per uzytkownik (np. `str(user_id)`)
- Wysylaj `interface` (`<klient>:<uzytkownik>`) -- identyfikuje uzytkownika w audit logu i w VIBE
- Obsluz status `confirm` -- zapytaj uzytkownika i wyslij odpowiedz
- Obsluz `attachment` (pokaz obraz albo zapisz plik) i `event` typu `progress` (pokaz na biezaco)
- Czytaj linie z limitem >= 32 MiB
- Obsluz dlugie odpowiedzi -- podziel na mniejsze wiadomosci jesli platforma ma limit
- Alerty: osobne, trwale polaczenie z `subscribe` i ponawianiem po rozlaczeniu
