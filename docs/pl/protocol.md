# Protokół komunikacji -- Pipe

Pipe v0.50.0

## Opis

Backend nasłuchuje równocześnie na:
1. Unix socket (`/tmp/vps-agent.sock`) -- dla klientów na tym samym serwerze (Telegram bot)
2. TCP `127.0.0.1:7379` -- dla SSH tunnel z laptopa (CLI)

Komunikacja odbywa się przez prosty protokół JSON lines (jedna linia JSON = jedna wiadomość).
W trybie Kubernetes dostęp do portu TCP daje `kubectl port-forward svc/pipe 7379:7379`.

## Format wiadomości

### Żądanie -- wiadomość użytkownika

```json
{
  "message": "tekst wiadomosci",
  "session_id": "uuid-per-uzytkownik",
  "interface": "cli:<user>|telegram:<user_id>|web:<user>",
  "token": "tajny-token",
  "attachments": [{"name": "zrzut.png", "mime": "image/png", "data": "<base64>"}]
}
```

`attachments` jest opcjonalne (pliki, zdjęcia, zrzuty ekranu); z załącznikami `message` może być puste. Limity:
10 MB na plik, 20 MB i 10 plików na wiadomość -- przekroczenie albo zły base64 kończy się ramka błędu bez wywołania
agenta. Rodzaj backend ustala z zawartości: obraz idzie do modelu jako `image_url`, tekst jest wklejany do wiadomości
(po redakcji sekretów), inne pliki zapisuje w `DATA_DIR/uploads/` i podaje agentowi ścieżkę. Linia żądania może
mieć do 32 MiB (`READ_LIMIT` serwera).

### Żądanie -- potwierdzenie operacji

```json
{
  "confirm": true,
  "session_id": "uuid-per-uzytkownik",
  "token": "tajny-token"
}
```

### Żądanie -- komenda klienta

Komendy "/" klientów (Telegram, CLI) wysyłają `{"command": ...}`:

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

| `command` | Dodatkowe pola | Odpowiedź |
|---|---|---|
| `list_skills` | -- | jedna linia z `"data": {"skills": [{"name", "description", "command"}]}` |
| `server_md` | -- | `"data": {"content": "..."}` (pusty, gdy SERVER.md nie istnieje) |
| `scan_server` | -- | streaming: agent bada serwer, tworzy/aktualizuje SERVER.md i DIRECTORY |
| `status` | -- | stan serwera **bez LLM**: ramka z tekstem w `response` (dla starszych klientów), potem `done` z `"data"` jak `overview` |
| `overview` | -- | `"data": {"hostname", "status": "ok\|warning\|critical", "headline", "facts", "gauges": [{"id", "label", "pct", "level": "ok\|warn\|crit", "value"}], "series": {"cpu", "mem", "disk"}, "alerts", "containers", "sites", "projects", "unread", "usage": {"tokens", "cost", "cached", "prompt", "saved", "calls"}, "text"}` -- pulpit **bez LLM** |
| `feed` | `args`: puste albo `seen` (przeczytane); opcjonalnie `since` (znacznik czasu) | `"data": {"items": [{"id", "ts", "at", "type", "severity", "title", "text", "key", "kind", "actions": [{"index", "label", "kind", "command"?, "args"?, "admin"?, "primary"?}], "push", "held", "done", "unread"}], "unread", "seen", "text", "prefs"}` -- skrzynka: co Pipe wysłał sam |
| `feed_act` | `id` wiadomości, `action` (indeks) | wykonuje przycisk: `snooze` / `mute` / `dismiss` -> `"data": {"id", "done", "text"?, "muted"?}`; `message` -> streaming rozmowy z agentem; `command` -> jak ta komenda (`cleanup`, `patch`, `investigate`...). Akcja z `admin` -- tylko administrator; wykonana akcja (`done`) drugi raz -> `error` |
| `prefs` | opcjonalnie `level` (`all\|important\|critical`), `quiet` (`22:00-07:00` albo `off`), `per_day` (0-50), `args: unmute <rodzaj>\|all` | `"data": {"level", "quiet", "per_day", "muted", "text", "can_edit"}` -- ustawienia powiadomień; zmiana tylko dla administratora |
| `install` | `args`: `skill\|plugin <zrodlo> [filtr]`; potem `token` + `execute: true` | `/pobierz` bez LLM: podgląd `"data": {"token", "text", "skills", "servers", "empty"}` (repozytorium sklonowane i usunięte), po TAK `"data": {"text"}`; token ważny 15 min, jednorazowy. Tylko admin; w trybie obserwacji bez serwerów MCP |
| `mcp_servers` | `args`: puste (stan), `dodaj <dodatek z katalogu>`, `dodaj <nazwa> <npx ...\|uvx ...\|docker run ...\|URL> [ZMIENNA=${MCP_X}]`, `usun <nazwa>`, `wlacz\|wylacz <nazwa>`, `odswiez`, `klucz` (z `name` = `MCP_...` i `key` = wartość), `zapomnij <MCP_...>` (też `add`/`remove`/`enable`/`disable`/`reload`/`key`/`forget`) | okno `/mcp`: `"data": {"text", "servers": [{"name", "status", "error", "description", "source", "addon", "enabled", "runs", "version", "tools": [{"name", "read", "ask", "description"}], "secrets": [{"name", "set"}]}], "catalog": [{"name", "title", "description", "category", "homepage", "login", "installed", "ready", "runs", "secrets": [{"name", "label", "help", "url", "set"}]}], "can_edit"}` -- wartości sekretów nigdy. Zmiany: tylko admin, dodanie i włączenie nie w trybie obserwacji |
| `web` | `args`: puste (stan), `wlacz [tunel\|bezposrednio] [port] [domena]` / `on` (`tunel` = `wlacz tunel`), `wylacz` / `off`, `wpusc <kod> [podglad]` / `approve`, `odrzuc <kod>` / `deny`, `odwolaj <numer\|nazwa\|wszystkie>` / `revoke`; przyciski skrzynki podają `id` prośby i `role` | Pipe w przeglądarce pod publicznym adresem (`core/webpublic.py`, `core/webaccess.py`): `"data": {"on", "url", "mode" (direct\|tunnel), "devices", "pending", "text", "can_edit"}`; `wlacz` wysyła ramki `progress` i dodaje `ok`. Stan widzi każdy (urządzenia i prośby -- administrator), zmiany tylko administrator. **Bez LLM** |
| `cleanup` | -- | zwolnij miejsce **bez LLM**: ramki `progress`, potem `"data": {"freed", "text", "steps", "skipped", "exit_code"}`. Odrzucane: viewer, tryb obserwacji |
| `projects` | `args`: puste albo `logs` / `stop` / `start` / `remove` (z `execute: true`, inaczej podgląd), `name` | `"data": {"projects": [{"name", "path", "url", "stack", "run", "status", "detail"}], "workspace", "text", "can_edit"}`; z `args` -- `"data": {"name", "text"}`. Zmiany: administrator, nie w trybie obserwacji |
| `run_skill` | `name` (nazwa albo komenda skilla), opcjonalnie `args` | streaming |
| `diagram` | opcjonalnie `args` (tytuł) | mapa infrastruktury **bez LLM**: ramka z `attachment`, potem `done` z `"data": {"summary": "..."}` |
| `directory` | -- | `"data": {"entries": [{"path", "kind", "description", "remote", "branch", ...}], "text": "..."}` |
| `new_chat` | -- | `"data": {"cleared": true\|false}`: usuwa historię sesji z pamięci i z dysku (`/nowa`); pamięć serwera zostaje |
| `vibe` | `args`: puste albo `reset` | `"data": {"content": "..."}` albo `{"content": "", "reset": true\|false}` (VIBE użytkownika z `interface`) |
| `alerts` | -- | `"data": {"active": [alert], "recent": [zdarzenie], "enabled": bool}` |
| `targets` | -- | `"data": {"targets": ["opis celu", ...]}` |
| `routines` | -- | `"data": {"routines": ["opis rutyny", ...]}` |
| `skill` | `name` (nazwa albo komenda) | `"data": {"name", "description", "content", "command"}` -- treść skilla do podglądu |
| `graph` | -- | `"data": {"root", "hostname", "views": {"fleet" \| "host" \| "p:<projekt>": {"title", "parent", "nodes": [{id, kind, label, sub, state, opens?, meta, alerts?}], "edges": [{from, to, kind, label?}]}}, "index": {id: [wezel na kazdym poziomie]}}` -- schemat jako dane (interfejs webowy) |
| `journal_changes` | `id` | `"data": {"id", "command", "status", "undoable", "files": [{path, status: added\|deleted\|modified\|unchanged\|unknown, diff, note}], "inverse", "notes"}` -- różnica "przed -> teraz" z kopii dziennika, sekrety zredagowane |
| `providers` | -- | `"data": {"providers": [{id, name, model, default_model, requires_key, has_key, key_source: web\|env\|"", ready, key_url, notes, base, active}], "active": {id, name, model}, "chosen": bool, "can_edit": bool}` -- providerzy LLM bez kluczy; `ready` = ma klucz |
| `provider_models` | `name` (id providera), `key` (opcjonalnie -- nowy klucz do sprawdzenia) | `"data": {"models": [...], "total"}` -- aktualne modele czatu providera; odrzucony klucz = `error`. Tylko administrator |
| `provider_set` | `name`, `key` (opcjonalnie), `model` (opcjonalnie) | jak `providers` -- zapisuje klucz (po sprawdzeniu u providera) i model, przełącza agenta, ustawia `chosen`. Tylko administrator |
| `provider_forget` | `name` | jak `providers` -- usuwa klucz dodany z interfejsu; provider bez klucza przestaje być aktywny. Tylko administrator |
| `yolo` | `args`: `on` \| `off` (opcjonalnie; także `wlacz`/`wylacz`, `tak`/`nie`) | `"data": {"yolo": bool, "can_edit": bool, "text"}` -- bez `args` stan, z `args` wlacza/wylacza tryb YOLO **tej sesji**: operacje wymagające potwierdzenia wykonują się od razu (przez bezpiecznik i dziennik; w strumieniu zamiast `[POTWIERDZ]` przychodzi zdarzenie `progress` „YOLO — ...”). Zmiana tylko dla administratora; nie przetrwa restartu backendu |
| `view` | `args`: `prosty` \| `techniczny` (opcjonalnie; także `simple`, `technical` albo opis zwykłymi słowami, np. *"przypomnienia i maile"*) | `"data": {"view": "simple" \| "tech", "chosen": bool, "views", "can_edit": bool, "text"}` -- bez `args` stan, z `args` ustawia widok całego agenta (`DATA_DIR/view.json`, `core/views.py`); subskrybenci dostają zdarzenie `view`. Nieznany opis -> błąd z oboma nazwami. Zmiana tylko dla administratora |
| `language` | `args`: `pl` \| `en` (opcjonalnie; także `polski`, `english`, `angielski`) | `"data": {"lang", "chosen": bool, "languages": ["pl", "en"], "can_edit": bool, "text"}` -- bez `args` stan, z `args` przełącza język całego agenta (prompty, opisy narzędzi, raporty, komunikaty) bez restartu i zapisuje wybór w `DATA_DIR/language.json`; subskrybenci dostają zdarzenie `language`. Zmiana tylko dla administratora |
| `reminders` | `cancel`: id (opcjonalnie), `claim`: true (opcjonalnie) | `"data": {"text", "reminders": [{id, due, kind, text, fired}], "claimed": [zdarzenia reminder], "cancelled"}` -- `claim` odbiera przypomnienia, które odpaliły, gdy nikt nie subskrybował (CLI) |
| `history` | -- | `"data": {"entries": ["linia audit logu", ...]}` (ostatnie 15) |
| `update` | `args`: puste albo `sprawdz` / `check` | streaming: agent wywołuje narzędzie `pipe_update` (`apply` z potwierdzeniem albo `check`) |
| `investigate` | `id` alertu | streaming: agent bada alert czuwania; backend dołącza zmiany na serwerze z ostatniej doby |
| `changes` | `args`: okres (`24h`, `3d`) | `"data": {"text": "...", "hours": 24}` -- co się zmieniło (migawki), **bez LLM** |
| `chart` | `args`: `load\|ram\|dysk` i okres (`ram 7d`) | ramka z `attachment` (PNG), potem `"data": {"summary", "metric", "image": bool}` |
| `health` | -- | `"data": {"text": "..."}` -- certyfikaty, strony, DNS, backupy (sprawdzenia bez konfiguracji) |
| `digest` | -- | ramka z `attachment` (wykres load 24 h), potem `"data"` jak zdarzenie `digest` (poniżej) |
| `mcp` | `rpc`: jedna wiadomość JSON-RPC MCP | `"data": {"rpc": odpowiedz \| null}` -- most MCP (CLI `--mcp`); tożsamość i rola z tokenu |
| `approvals` | -- | `"data": {"pending": [zdarzenie approval]}` |
| `approve` | `id`, `decision` (bool) | tylko admin; zatwierdzenie wykonuje operację przez bezpiecznik, `"data": {"id", "status", "text"}` |
| `incidents` | -- | `"data": {"text", "incidents": [...]}` -- pamięć incydentów (co się zdarzało, ustalenia, co pomogło) |
| `transcribe` | `audio` (base64, maks. 10 MB; OGG, WAV, MP3...), `filename` | `"data": {"text": "..."}` -- transkrypcja wiadomości głosowej (STT) |
| `audit` | -- | `"data": {"score", "grade", "findings": [{"id", "severity", "title", "detail", "fix", "command", "host_only"}], "passed", "unknown", "text"}` -- **bez LLM** |
| `welcome` | -- | ramka z `attachment` (mapa), potem `"data"` jak zdarzenie `welcome` |
| `journal` | -- | `"data": {"entries": [{"id", "summary", "undoable", "status"}], "text": "..."}` -- dziennik zmian |
| `vulns` | -- | `"data": {"text", "view"}` -- raport podatności w Markdown (`view`: to samo jako dane dla `pipe web`, `vulns.report_view`) (pakiety, zdalne serwery, obrazy, aplikacje) z wyjaśnieniami; brakujące pisze model (najwyżej `EXPLAIN_WAIT` s), bez modelu opis z OSV |
| `patch` | `scope`: puste / `all` (wszystko), `images`/`obrazy`, `system`/`serwer`, `apps`/`aplikacje`, `servers`/`serwery`, nazwa celu ssh, `urgent`/`pilne` (pilne pakiety tego serwera) | łata od razu (`core/patching.py`): ramki `progress` z kolejnymi krokami, potem nowy skan i `"data": {"text", "exit_code", "entry_id", "entries", "scope", "summary", "fixed", "unchanged", "freed", "output", "report", "parts"}` (`unchanged` -- bez zmian po instalacji, nie wraca przez dobę; `freed` -- bajty zwolnione przy sprzątaniu; `report` -- stan po łatce jak `view` z `vulns`). Nic do załatania: `"data": {"nothing": true, "text", ...}`. Komendy składa serwer z bieżącego skanu. Łatanie działa w tle: drugie `patch` w trakcie dostaje ten sam postęp i wynik, a po rozłączeniu klienta wynik przychodzi jako przypomnienie `message`. Pola `execute` i `patch_token` (podgląd sprzed v0.38) są ignorowane. Odrzucane: viewer, tryb obserwacji; pakiety i obrazy tego serwera tylko w trybie native i docker (z kontenerem pomocniczym). **Bez LLM** |
| `undo` | opcjonalnie `id`; `execute: true` | bez `execute`: `"data": {"id", "undoable", "preview"}` (ostatni wpis do cofnięcia, gdy brak `id`); z `execute` i `id`: cofnięcie, `"data": {"id", "text"}`. **Bez LLM** |
| `usage` | -- | `"data": {"today", "history", "month", "text"}` -- tokeny i koszt LLM |
| `subscribe` | `since`: `ts` ostatniego widzianego zdarzenia (opcjonalnie) | połączenie zostaje otwarte; najpierw zdarzenia opublikowane po `since`, potem zdarzenia czuwania (poniżej) do rozłączenia klienta |

Treść wiadomości dla agenta (`scan_server`, `run_skill`, `investigate`, akcja `message` ze skrzynki) buduje backend --
klient wysyła tylko komendę. Wiadomości zbudowane przez backend nie uczą VIBE.
Pole `command` skilla to nazwa zgodna z zasadami Telegrama (małe litery, cyfry, `_`, do 32 znaków). Jest puste,
gdy nazwa skilla jest zarezerwowana (`status`, `mapa`, `server`, `skille`, `katalogi`, `vibe`, `alerty`,
`cele`, `rutyny`, `zmiany`, `wykres`, `zdrowie`, `raport`, `koszt`, `cofnij`, `dziennik`, `audyt`, ...) albo po skróceniu koliduje z innym skillem.

### Odpowiedź

```json
{
  "response": "tekst odpowiedzi",
  "status": "ok|confirm|error",
  "done": false
}
```

Serwer wysyła wiele linii JSON (streaming). Ostatnia linia ma `"done": true`.
Ramka może zawierać dodatkowo (wtedy `response` jest pusty, więc starsi klienci ją pomijaja):

**Załącznik** -- plik dla użytkownika, np. diagram:

```json
{"response": "", "status": "ok", "done": false, "attachment": {
  "name": "mapa-infrastruktury.png", "mime": "image/png", "caption": "Mapa infrastruktury",
  "data": "<base64>", "source": "<kod Mermaid>", "text": "<podglad ASCII albo pusty>"}}
```

**Postęp** -- status pośredni (np. co robi worker), do pokazania na bieżąco, nie wchodzi do odpowiedzi:

```json
{"response": "", "status": "ok", "done": false, "event": {"type": "progress", "text": "web-1 $ uptime", "source": "worker:web-1"}}
```

Ramki z diagramami mają setki KB -- klient musi czytać linie z większym limitem niż domyślne 64 KiB
asyncio (`open_connection(..., limit=32 * 1024 * 1024)`).

### Zdarzenia czuwania (`subscribe`)

Po `{"command": "subscribe"}` serwer wysyła `{"event": {"type": "subscribed", "lang": "pl|en", "lang_chosen": bool,
"view": "simple|tech", "view_chosen": bool, "ts": 1761679200.5}}` (`lang_chosen` -- język wybrano komendą `language`,
klient powinien go przejąć; `view_chosen` -- tak samo dla widoku), a potem, bez `done`:

```json
{"response": "", "status": "ok", "done": false, "event": {"type": "language", "lang": "en", "at": "..."}}
```

(język Pipe zmieniony z dowolnego kanału -- klient przełącza swoje komunikaty; zdarzenie nie trafia do historii `alerts`;
tak samo `{"type": "view", "view": "simple"}` po zmianie widoku)

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
  "label": "docker restart web", "nodes": ["c:web"], "ok": null, "entry": "", "workers": {},
  "output": "web", "full": "...", "code": 0, "auto": true}}
```

Zdarzenia `activity` dostaje każdy klient w strumieniu rozmowy (poza rola viewer) -- mówią, co agent robi i na
których węzłach schematu (`graph`). `wait` = czeka na potwierdzenie; `end` ma `ok` (true/false/null po odmowie)
i `entry` -- identyfikator wpisu dziennika, gdy zmiana została wykonana. Przy komendach (`execute_command`,
`remote_exec`, `git_command`, `cron_manage`, `write_file`) `end` niesie też wycinek wyniku: `output` (do 3 linii:
przy błędzie linie z błędem i ostatnia, przy sukcesie koniec, plus alarm bezpiecznika), `full` (do 60 linii / 4000
znaków, do rozwinięcia) i `code` (kod wyjścia). To ten sam wynik, który dostał model, już po redakcji sekretów;
w widoku prostym tych pól nie ma. `auto: true` -- zmiana wykonana bez pytania (samodzielność, `docs/security.md`).
Klient, który zna tylko `progress`, może zdarzenia `activity` pominąć.

`to` to interfejs, który ustawił przypomnienie -- klient decyduje, komu je pokazać. Dla `kind: task`
pole `report` zawiera raport workera.

```json
{"response": "", "status": "ok", "done": false, "event": {
  "type": "digest", "title": "Raport vps1 -- 29.09.2026 07:00", "text": "<calosc jako tekst>",
  "sections": [{"title": "Stan", "lines": ["..."]}, {"title": "Zmiany od wczoraj", "lines": ["..."]}],
  "attachment": {"name": "wykres-load.png", "mime": "image/png", "data": "<base64>", "...": "..."}, "at": "..."}}
```

`{"type": "investigation", "key", "title", "report", "auto", "alert_id", "severity", "fix_message"}` -- ustalenia
workera, który sam zbadał krytyczny alert czuwania (`AUTO_INVESTIGATE`) albo alert z webhooka (`WEBHOOK_INVESTIGATE=1`);
obie drogi to jedno `Watcher.auto_investigate()`, a `fix_message` to treść przycisku *Napraw*.
`history` w alercie -- ostatnie wystąpienie tego samego problemu (pamięć incydentów).

`{"type": "insight", "key", "kind", "severity": "info|warning", "title", "text"}` -- podpowiedź bez LLM (prognoza
zapełnienia dysku, miejsce do odzyskania, restarty kontenera, czekający restart serwera, rosnący RAM).
`{"type": "notice", "title", "text"}` -- podsumowanie tego, co czekało w ciszy nocnej.
`{"type": "login", "id", "code", "device", "ip", "title", "text"}` -- ktoś przed stroną `/web` prosi o dostęp;
przyciski skrzynki (*Wpuść*, *Tylko podgląd*, *Odrzuć*) wołają komendę `web`. Zawsze z powiadomieniem, tylko dla
administratorów.

**Skrzynka.** Zdarzenia `alert`, `insight`, `digest`, `welcome`, `routine`, `investigation` i `notice` trafiają do
skrzynki i mają dodatkowo `feed_id`, `push` (czy powiadomić teraz: poziom, cisza nocna, limit podpowiedzi), `held`
(czeka na koniec ciszy) i `actions` (przyciski: `index`, `label`, `kind`, `admin`...). Klient powiadamia tylko przy
`push: true`, a przycisk wysyła `feed_act` z `feed_id` i `index`. Brak `push` (starszy backend) = powiadom.

`{"type": "approval", "id", "command", "target", "requested_by", "reason", "plan", "status"}` -- zewnętrzny agent
(MCP) czeka na zgodę administratora; klient pokazuje przyciski i wysyła `approve`.

`{"type": "welcome", "title", "sections", "score", "grade", "text", "attachment"?}` -- raz po instalacji, gdy
podłączy się pierwszy subskrybent.

`state`: `new` -- nowy albo eskalowany alert, `resolved` -- problem minął, `event` -- zdarzenie jednorazowe
(nowy publiczny port, zatrzymany kontener). `investigate` z `id` prosi agenta o zbadanie alertu.
Klucze alertów mają prefiks grupy: `disk:`, `memory`, `load`, `container:`, `port:` (co `WATCH_INTERVAL`)
oraz `cert:`, `site:`, `dns:`, `backup:` (sprawdzenia bez konfiguracji, co `CHECKS_INTERVAL`).
`vuln:system` -- pilne podatności z poprawką w pakietach systemu, `vuln:images` -- w obrazach kontenerów,
`vuln:servers` -- na zdalnych serwerach (cele ssh), `vuln:apps` -- krytyczne w zależnościach aplikacji (trwałe;
wysyłane ponownie, gdy dojdzie nowa).
`hook:<zrodlo>:<nazwa>` -- alerty z webhooków (trwałe do komunikatu `resolved`). `auth:ssh` (seria nieudanych
logowań) jest trwały; `security:<sekcja>:<klucz>`, `auth:breach:...` i `auth:login:...`
to zdarzenia jednorazowe.
`digest` przychodzi raz dziennie o `DIGEST_TIME`.

### Autoryzacja tokenem

Pole `token` jest wymagane, gdy skonfigurowano jakikolwiek token: `AGENT_TOKEN` (rola admin),
`AGENT_VIEWER_TOKEN` (rola viewer) albo tokeny klientów (`python3 -m backend.tokens add NAZWA --role admin|viewer`,
w `data/tokens.json` sam skrót SHA-256). Bez żadnego tokenu backend przyjmuje każde żądanie (rola admin).

Rola **viewer** (tylko odczyt): `{"confirm": true}` i `undo` z `execute` są odrzucane, narzędzia zmieniające stan
zwracają modelowi odmowę (klient nie dostaje pytania o TAK). Sesja należy do tożsamości, która ją założyła
(token admina, viewera albo nazwany token) -- żądanie z innym tokenem i tym samym `session_id` dostaje
`Ta sesja nalezy do innego klienta.`

Token jest sprawdzany dla **każdego** typu żądania -- również dla potwierdzeń
(`confirm`), żeby nieuwierzytelniony klient nie mógł zatwierdzić operacji
oczekującej w cudzej sesji. Przy błędnym tokenie backend odpowiada:

```json
{"response": "Blad: Nieprawidlowy token autoryzacji.", "status": "error", "done": true}
```

## Stany odpowiedzi

| Status | Znaczenie |
|--------|-----------|
| `ok` | Standardowa odpowiedź agenta |
| `confirm` | Agent czeka na potwierdzenie użytkownika |
| `error` | Błąd agenta lub odmowa |

## Przebieg sesji

1. Klient nawiązuje połączenie (Unix socket lub TCP)
2. Klient wysyła żądanie (linia JSON + `\n`)
3. Serwer odpowiada jedna lub wieloma liniami JSON
4. Ostatnia linia ma `"done": true`
5. Jeśli `"status": "confirm"` -- klient pyta użytkownika o potwierdzenie
6. Klient wysyła `{"confirm": true|false, "session_id": "..."}` 
7. Serwer kontynuuje przetwarzanie i wysyła kolejne odpowiedzi

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

## Wymagania dla nowych klientów

- Jeden `session_id` per użytkownik (np. `str(user_id)`)
- Wysyłaj `interface` (`<klient>:<uzytkownik>`) -- identyfikuje użytkownika w audit logu i w VIBE
- Obsłuż status `confirm` -- zapytaj użytkownika i wyślij odpowiedź
- Obsłuż `attachment` (pokaż obraz albo zapisz plik) i `event` typu `progress` (pokaż na bieżąco)
- Czytaj linie z limitem >= 32 MiB
- Obsłuż długie odpowiedzi -- podziel na mniejsze wiadomości jeśli platforma ma limit
- Alerty: osobne, trwałe połączenie z `subscribe` i ponawianiem po rozłączeniu; wyślij `"since"` z `ts` ostatniego
  zdarzenia, które widziałeś, a serwer najpierw dośle to, co cię ominęło (np. wynik `/aktualizuj`, który przychodzi
  zaraz po restarcie backendu). `pipe web` robi to przez `id:` / `Last-Event-ID` w SSE


## Webhooki (HTTP)

Osobny port (`WEBHOOK_PORT`, domyślnie wyłączony), przyjmuje alerty z innych systemów:

```
POST /hook/alertmanager | /hook/grafana | /hook/uptime-kuma | /hook/github | /hook/generic
Authorization: Bearer <WEBHOOK_TOKEN>        (albo ?token=..., albo podpis X-Hub-Signature-256 GitHuba)
Content-Type: application/json

202 {"ok": true, "alerts": 2, "events": 1}
GET /health -> 200 {"ok": true}
```

Format ogólny (`/hook/generic`): `{"title", "message", "severity": "critical|warning", "status": "firing|resolved",
"name"}`. Limit tresci 1 MiB. Bez `WEBHOOK_TOKEN` serwer webhooków nie startuje.
