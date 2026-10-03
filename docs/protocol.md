# Protokol komunikacji -- Pipe

Pipe v0.22.1

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
| `skill` | `name` (nazwa albo komenda) | `"data": {"name", "description", "content", "command"}` -- tresc skilla do podgladu |
| `graph` | -- | `"data": {"root", "hostname", "views": {"fleet" \| "host" \| "p:<projekt>": {"title", "parent", "nodes": [{id, kind, label, sub, state, opens?, meta, alerts?}], "edges": [{from, to, kind, label?}]}}, "index": {id: [wezel na kazdym poziomie]}}` -- schemat jako dane (interfejs webowy) |
| `journal_changes` | `id` | `"data": {"id", "command", "status", "undoable", "files": [{path, status: added\|deleted\|modified\|unchanged\|unknown, diff, note}], "inverse", "notes"}` -- roznica "przed -> teraz" z kopii dziennika, sekrety zredagowane |
| `providers` | -- | `"data": {"providers": [{id, name, model, default_model, requires_key, has_key, key_source: web\|env\|"", ready, key_url, notes, base, active}], "active": {id, name, model}, "chosen": bool, "can_edit": bool}` -- providerzy LLM bez kluczy; `ready` = ma klucz |
| `provider_models` | `name` (id providera), `key` (opcjonalnie -- nowy klucz do sprawdzenia) | `"data": {"models": [...], "total"}` -- aktualne modele czatu providera; odrzucony klucz = `error`. Tylko administrator |
| `provider_set` | `name`, `key` (opcjonalnie), `model` (opcjonalnie) | jak `providers` -- zapisuje klucz (po sprawdzeniu u providera) i model, przelacza agenta, ustawia `chosen`. Tylko administrator |
| `provider_forget` | `name` | jak `providers` -- usuwa klucz dodany z interfejsu; provider bez klucza przestaje byc aktywny. Tylko administrator |
| `yolo` | `args`: `on` \| `off` (opcjonalnie; takze `wlacz`/`wylacz`, `tak`/`nie`) | `"data": {"yolo": bool, "can_edit": bool, "text"}` -- bez `args` stan, z `args` wlacza/wylacza tryb YOLO **tej sesji**: operacje wymagajace potwierdzenia wykonuja sie od razu (przez bezpiecznik i dziennik; w strumieniu zamiast `[POTWIERDZ]` przychodzi zdarzenie `progress` „YOLO — ...”). Zmiana tylko dla administratora; nie przetrwa restartu backendu |
| `language` | `args`: `pl` \| `en` (opcjonalnie; takze `polski`, `english`, `angielski`) | `"data": {"lang", "chosen": bool, "languages": ["pl", "en"], "can_edit": bool, "text"}` -- bez `args` stan, z `args` przelacza jezyk calego agenta (prompty, opisy narzedzi, raporty, komunikaty) bez restartu i zapisuje wybor w `DATA_DIR/language.json`; subskrybenci dostaja zdarzenie `language`. Zmiana tylko dla administratora |
| `reminders` | `cancel`: id (opcjonalnie), `claim`: true (opcjonalnie) | `"data": {"text", "reminders": [{id, due, kind, text, fired}], "claimed": [zdarzenia reminder], "cancelled"}` -- `claim` odbiera przypomnienia, ktore odpalily, gdy nikt nie subskrybowal (CLI) |
| `history` | -- | `"data": {"entries": ["linia audit logu", ...]}` (ostatnie 15) |
| `update` | `args`: puste albo `sprawdz` / `check` | streaming: agent wywoluje narzedzie `pipe_update` (`apply` z potwierdzeniem albo `check`) |
| `investigate` | `id` alertu | streaming: agent bada alert czuwania; backend dolacza zmiany na serwerze z ostatniej doby |
| `changes` | `args`: okres (`24h`, `3d`) | `"data": {"text": "...", "hours": 24}` -- co sie zmienilo (migawki), **bez LLM** |
| `chart` | `args`: `load\|ram\|dysk` i okres (`ram 7d`) | ramka z `attachment` (PNG), potem `"data": {"summary", "metric", "image": bool}` |
| `health` | -- | `"data": {"text": "..."}` -- certyfikaty, strony, DNS, backupy (sprawdzenia bez konfiguracji) |
| `digest` | -- | ramka z `attachment` (wykres load 24 h), potem `"data"` jak zdarzenie `digest` (ponizej) |
| `mcp` | `rpc`: jedna wiadomosc JSON-RPC MCP | `"data": {"rpc": odpowiedz \| null}` -- most MCP (CLI `--mcp`); tozsamosc i rola z tokenu |
| `mcp_servers` | -- | `"data": {"servers": ["opis serwera MCP", ...]}` |
| `approvals` | -- | `"data": {"pending": [zdarzenie approval]}` |
| `approve` | `id`, `decision` (bool) | tylko admin; zatwierdzenie wykonuje operacje przez bezpiecznik, `"data": {"id", "status", "text"}` |
| `incidents` | -- | `"data": {"text", "incidents": [...]}` -- pamiec incydentow (co sie zdarzalo, ustalenia, co pomoglo) |
| `transcribe` | `audio` (base64, maks. ~2.5 MB), `filename` | `"data": {"text": "..."}` -- transkrypcja wiadomosci glosowej (STT) |
| `audit` | -- | `"data": {"score", "grade", "findings": [{"id", "severity", "title", "detail", "fix", "command", "host_only"}], "passed", "unknown", "text"}` -- **bez LLM** |
| `welcome` | -- | ramka z `attachment` (mapa), potem `"data"` jak zdarzenie `welcome` |
| `journal` | -- | `"data": {"entries": [{"id", "summary", "undoable", "status"}], "text": "..."}` -- dziennik zmian |
| `undo` | opcjonalnie `id`; `execute: true` | bez `execute`: `"data": {"id", "undoable", "preview"}` (ostatni wpis do cofniecia, gdy brak `id`); z `execute` i `id`: cofniecie, `"data": {"id", "text"}`. **Bez LLM** |
| `usage` | -- | `"data": {"today", "history", "month", "text"}` -- tokeny i koszt LLM |
| `subscribe` | -- | polaczenie zostaje otwarte; zdarzenia czuwania (ponizej) do rozlaczenia klienta |

Tresc wiadomosci dla agenta (`scan_server`, `status`, `run_skill`, `investigate`) buduje backend -- klient
wysyla tylko komende. Wiadomosci zbudowane przez backend nie ucza VIBE.
Pole `command` skilla to nazwa zgodna z zasadami Telegrama (male litery, cyfry, `_`, do 32 znakow). Jest puste,
gdy nazwa skilla jest zarezerwowana (`status`, `mapa`, `server`, `skille`, `katalogi`, `vibe`, `alerty`,
`cele`, `rutyny`, `zmiany`, `wykres`, `zdrowie`, `raport`, `koszt`, `cofnij`, `dziennik`, `audyt`, ...) albo po skroceniu koliduje z innym skillem.

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

Po `{"command": "subscribe"}` serwer wysyla `{"event": {"type": "subscribed", "lang": "pl|en", "lang_chosen": bool}}`
(`lang_chosen` -- jezyk wybrano komenda `language`, klient powinien go przejac), a potem, bez `done`:

```json
{"response": "", "status": "ok", "done": false, "event": {"type": "language", "lang": "en", "at": "..."}}
```

(jezyk Pipe zmieniony z dowolnego kanalu -- klient przelacza swoje komunikaty; zdarzenie nie trafia do historii `alerts`)

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "alert", "id": "3f2a9c1d0b", "key": "disk:/", "severity": "warning|critical",
  "title": "Dysk / zapelniony w 93%", "detail": "wolne 3.1 GB z 40 GB (/dev/sda1)",
  "since": "2026-09-24 14:02", "state": "new|resolved|event", "history": "", "at": "2026-09-24 14:02"}}
```

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "routine", "name": "poranny-przeglad", "status": "OK|PROBLEM", "report": "...", "at": "..."}}
```

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "reminder", "id": "6d1546", "kind": "message|task", "text": "...", "report": "",
  "to": "telegram:123", "set_at": "2026-10-01 20:02", "due": "2026-10-01 20:02:42", "at": "..."}}
```

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "activity", "id": "<id wywolania narzedzia>", "phase": "start|wait|end", "tool": "execute_command",
  "label": "docker restart web", "nodes": ["c:web"], "ok": null, "entry": "", "workers": {}}}
```

Zdarzenia `activity` dostaje tylko klient, ktorego `interface` zaczyna sie od `web` -- mowia, co agent robi i na
ktorych wezlach schematu (`graph`). `wait` = czeka na potwierdzenie; `end` ma `ok` (true/false/null po odmowie)
i `entry` -- identyfikator wpisu dziennika, gdy zmiana zostala wykonana.

`to` to interfejs, ktory ustawil przypomnienie -- klient decyduje, komu je pokazac. Dla `kind: task`
pole `report` zawiera raport workera.

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "digest", "title": "Raport vps1 -- 29.09.2026 07:00", "text": "<calosc jako tekst>",
  "sections": [{"title": "Stan", "lines": ["..."]}, {"title": "Zmiany od wczoraj", "lines": ["..."]}],
  "attachment": {"name": "wykres-load.png", "mime": "image/png", "data": "<base64>", "...": "..."}, "at": "..."}}
```

`{"type": "investigation", "key", "title", "report"}` -- raport workera, ktory sam zbadal alert z webhooka
(`WEBHOOK_INVESTIGATE=1`). `history` w alercie -- ostatnie wystapienie tego samego problemu (pamiec incydentow).

`{"type": "approval", "id", "command", "target", "requested_by", "reason", "plan", "status"}` -- zewnetrzny agent
(MCP) czeka na zgode administratora; klient pokazuje przyciski i wysyla `approve`.

`{"type": "welcome", "title", "sections", "score", "grade", "text", "attachment"?}` -- raz po instalacji, gdy
podlaczy sie pierwszy subskrybent.

`state`: `new` -- nowy albo eskalowany alert, `resolved` -- problem minal, `event` -- zdarzenie jednorazowe
(nowy publiczny port, zatrzymany kontener). `investigate` z `id` prosi agenta o zbadanie alertu.
Klucze alertow maja prefiks grupy: `disk:`, `memory`, `load`, `container:`, `port:` (co `WATCH_INTERVAL`)
oraz `cert:`, `site:`, `dns:`, `backup:` (sprawdzenia bez konfiguracji, co `CHECKS_INTERVAL`).
`hook:<zrodlo>:<nazwa>` -- alerty z webhookow (trwale do komunikatu `resolved`). `auth:ssh` (seria nieudanych
logowan) jest trwaly; `security:<sekcja>:<klucz>`, `auth:breach:...` i `auth:login:...`
to zdarzenia jednorazowe.
`digest` przychodzi raz dziennie o `DIGEST_TIME`.

### Autoryzacja tokenem

Pole `token` jest wymagane, gdy skonfigurowano jakikolwiek token: `AGENT_TOKEN` (rola admin),
`AGENT_VIEWER_TOKEN` (rola viewer) albo tokeny klientow (`python3 -m backend.tokens add NAZWA --role admin|viewer`,
w `data/tokens.json` sam skrot SHA-256). Bez zadnego tokenu backend przyjmuje kazde zadanie (rola admin).

Rola **viewer** (tylko odczyt): `{"confirm": true}` i `undo` z `execute` sa odrzucane, narzedzia zmieniajace stan
zwracaja modelowi odmowe (klient nie dostaje pytania o TAK). Sesja nalezy do tozsamosci, ktora ja zalozyla
(token admina, viewera albo nazwany token) -- zadanie z innym tokenem i tym samym `session_id` dostaje
`Ta sesja nalezy do innego klienta.`

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


## Webhooki (HTTP)

Osobny port (`WEBHOOK_PORT`, domyslnie wylaczony), przyjmuje alerty z innych systemow:

```
POST /hook/alertmanager | /hook/grafana | /hook/uptime-kuma | /hook/github | /hook/generic
Authorization: Bearer <WEBHOOK_TOKEN>        (albo ?token=..., albo podpis X-Hub-Signature-256 GitHuba)
Content-Type: application/json

202 {"ok": true, "alerts": 2, "events": 1}
GET /health -> 200 {"ok": true}
```

Format ogolny (`/hook/generic`): `{"title", "message", "severity": "critical|warning", "status": "firing|resolved",
"name"}`. Limit tresci 1 MiB. Bez `WEBHOOK_TOKEN` serwer webhookow nie startuje.
