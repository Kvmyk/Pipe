# Bezpieczenstwo -- Pipe

Pipe v0.16.2

## Model

Pipe daje modelowi jezykowemu dostep do serwera, wiec zasady bezpieczenstwa sa **w kodzie**, a nie
w prompcie. Model moze sie pomylic albo zostac zmanipulowany tresci przeczytanego pliku (prompt injection);
klasyfikator, potwierdzenia, redakcja sekretow i limity workerow dzialaja niezaleznie od tego, co model
"chce" zrobic.

| Warstwa | Co robi |
|---------|---------|
| Klasyfikator komend | `safe` / `confirm` / `forbidden` -- fail-closed |
| Potwierdzenie | pokazuje dokladnie ten napis, ktory zostanie wykonany |
| Redakcja sekretow | wyniki narzedzi sa czyszczone z kluczy i hasel przed wyslaniem do LLM |
| Workery i rutyny | wykonuja wylacznie `safe`; reszta wraca jako propozycja |
| Workspace | narzedzia plikowe widza tylko hosta i nie widza kluczy SSH, `/etc/shadow`, `/proc`... |
| Audit log | kazda operacja, bez tresci plikow |
| Token | `AGENT_TOKEN` wymagany dla kazdego zadania, takze potwierdzen i subskrypcji |

---

## Klasyfikacja komend (`backend/core/security.py`)

Kolejnosc:

1. **FORBIDDEN** na calym napisie -- lapie `curl ... | bash` i `$(rm -rf ~)`.
2. Podzial na segmenty po `;`, `|`, `||`, `&&`, `&` i nowych liniach -- **z uwzglednieniem cudzyslowow**
   (`grep 'a|b'` to jeden segment).
3. Kazdy segment: forbidden -> wrazliwe pliki (confirm) -> wzorce confirm -> rozpoznany odczyt (safe).
   Dopasowanie jest **tokenowe**: `ss` nie pasuje do `ssh`, `ps` do `psql`, `id` do `idiotic-cmd`.
4. Przekierowanie zapisu (`>`, `>>` poza `/dev/null` i `2>&1`) albo dynamiczna konstrukcja (`$(...)`,
   `${...}`, `$'...'`, backtick, `<(...)`) -> **confirm**, nawet gdy segmenty sa odczytami.
5. Wszystko, czego klasyfikator nie rozpoznaje -> **confirm** (fail-closed).

### SAFE -- rozpoznane odczyty

Pliki i tekst (`cat`, `grep`, `ls`, `find`, `du`, `df`, `jq`...), system (`ps`, `free`, `uptime`,
`systemctl status`, `journalctl`), siec (`ss`, `ip addr`, `ping`, `dig`, `curl` GET), Docker
(`ps`, `logs`, `inspect`, `stats`, `compose ps/logs/config`), Git (`status`, `log`, `diff`, `show`, listowanie
galezi/tagow), Kubernetes (`get`, `describe`, `logs`, `top`, `rollout status`), Helm (`list`, `status`, `get`).

Argumenty, ktore zmieniaja stan, sa sprawdzane osobno:

| Komenda | Wymaga potwierdzenia, gdy... |
|---------|------------------------------|
| `git` | podkomenda nie jest odczytem, flaga uruchamiajaca program (`-c core.pager=`, `--upload-pack`, `--receive-pack`, `--exec`, `-O`/`--open-files-in-pager`) albo piszaca do pliku (`-o`/`--output`), `branch -D`, `branch nowa`, `tag v1`, `remote add`, `config` bez `--get/--list` |
| `find` | `-exec`, `-execdir`, `-ok`, `-delete`, `-fprint` |
| `curl` | `-o/-O`, `-T`, `-d/--data`, `-F`, `--json`, `-D`, `--trace*`, `-X`/`--request` (takze `--request=`, `-X<METODA>`) inne niz GET/HEAD, **albo host inny niz localhost/prywatny** (eksfiltracja) -- takze proxy (`-x`, `--proxy`, `--socks5`), `--resolve`/`--connect-to` i adres zapisany jedna liczba (`http://134744072/`) |
| `sort` | `-o`/`--output`, `--compress-program` (uruchamia program) |
| `journalctl` | `--vacuum-*`, `--rotate`, `--flush` |
| `kubectl get secret` | `-o yaml/json/jsonpath/go-template/custom-columns` (takze `-o=...`) albo `--template` -- rowniez gdy sekrety sa na liscie zasobow (`get cm,secret`) -- tresc sekretu trafilaby do LLM |

Odczyty tych sciezek tez wymagaja zgody, bo wyslalyby sekrety do providera:
`/etc/shadow`, klucze SSH, `*.pem`/`*.key`, `.env` (takze wzgledne `cat .env`), **`/proc/*/environ` i `/proc/*/root`**
(dowolny PID, `self` i globy; przy `pid: host` to zmienne i katalogi domowe procesow hosta -- obejscie
read-only `/hostfs`), oraz globy w katalogach z sekretami (`cat /etc/s*adow`, `cat /root/.ssh/id_*`).
Wzorce sa sprawdzane takze po usunieciu cudzyslowow i backslashy, wiec `cat /etc/sha""dow` nie przejdzie.
Te same pliki czytane przez `read_file` tez wymagaja zgody.

### CONFIRM -- zmiany stanu

`rm`, `mv`, `cp`, `chmod`, `chown`, `kill`, `systemctl start/stop/restart/enable...`, `apt install/upgrade/remove`,
`docker run/exec/start/stop/restart/rm/compose up/down`, `git push/commit/pull/reset...`, `kubectl apply/delete/scale/exec...`,
`helm install/upgrade/uninstall`, `ufw`, `iptables` (poza listowaniem), `reboot`, `crontab` (poza `-l`) --
oraz kazda nieznana komenda. Odczyt plikow z sekretami (`/etc/shadow`, klucze SSH, `*.pem`, `*.key`, `.env`)
tez wymaga zgody.

### FORBIDDEN -- zawsze odrzucane

`rm -rf /` (i warianty: `-fr`, `/*`, `~`, `"/"`, `/hostfs`), `rm --no-preserve-root`, `dd if=`, `mkfs`,
`wipefs`, `shred`, zapis do `/etc/passwd`, `/etc/shadow`, `/boot/`, `/dev/sd*`, fork bomb,
`curl|wget ... | sh`, `base64 ... | sh`, `python -c ...exec`, `kubectl delete ns kube-system`.
Odmowa nie wraca do modelu jako blad do ponowienia -- dostaje informacje, zeby nie probowal obejsc.

Regresje obejsc znalezionych przy przebudowie: `backend/tests/test_security_hardening.py`,
`backend/tests/test_review_fixes_v091.py`.

---

## Redakcja sekretow

Kazdy wynik narzedzia (komendy, plik, raport workera) przechodzi przez `memory.redact_secrets()` przed
wyslaniem do providera LLM:

- klucze prywatne (caly blok PEM), klucze AWS, `sk-...`, tokeny GitHub, Slack, Google, botow Telegrama, **JWT**,
- `NAZWA_Z_PASSWORD/SECRET/TOKEN/API_KEY=wartosc` -- klucz zostaje, wartosc znika
  (`DB_PASSWORD=[ZREDAGOWANO: sekret]`), wiec agent wie, ze zmienna istnieje. Dopasowanie nie przechodzi
  przez znaki NUL, wiec nie zjada calego `/proc/<pid>/environ`,
- **dane logowania w URL-ach** (`postgres://user:haslo@host` -> `postgres://user:[ZREDAGOWANO]@host`), w tym
  `git remote -v` i `docker inspect`,
- **hashe hasel z `/etc/shadow`** (`$6$...`).

Agent nie moze zapisac `write_file` tresci ze znacznikiem `[ZREDAGOWANO` -- inaczej przepisujac plik `.env`
zniszczylby prawdziwe wartosci. Musi zmieniac taki plik punktowo (np. `sed -i`, z potwierdzeniem).
Wylaczenie: `REDACT_SECRETS=0` (niezalecane).

Wyniki dluzsze niz 24 000 znakow sa przycinane (poczatek + koniec), a historia sesji -- na granicy wiadomosci
uzytkownika, zeby para wywolanie/wynik narzedzia nigdy nie zostala rozdzielona.

---

## Potwierdzenia: co widzisz, to wykonasz

- Komenda w potwierdzeniu (`as_code()`) ma **zneutralizowane znaki sterujace i bidi** -- sekwencje ANSI, `\r`
  i przelaczniki kierunku pisma (U+202E) sa zamieniane na widoczny zapis `\xNN`, wiec nie ukryja roznicy
  miedzy tym, co widzisz, a tym, co sie wykona.
- `write_file` pokazuje **diff wzgledem obecnej tresci** (albo podglad nowego pliku), a przy plikach
  uruchamianych automatycznie (`.bashrc`, `authorized_keys`, cron, systemd, `.gitconfig`) dodaje ostrzezenie
  o trwalym dostepie.
- Potwierdzenie jest przypiete do `tool_call_id` w sesji; nowa wiadomosc zamiast TAK anuluje operacje.
- Plan bezpiecznika (kopie, sprawdzenia, weryfikacja) jest czescia potwierdzenia i jest przechowywany w nim --
  po TAK wykonywany jest dokladnie pokazany plan. Komendy sprawdzajace (`nginx -t`, `sshd -t`, `docker inspect`...)
  sa skladane z szablonow w kodzie, parametry przez `shlex.quote`.

## Role i tokeny

- `AGENT_TOKEN` -- admin, `AGENT_VIEWER_TOKEN` -- viewer, tokeny klientow z `python3 -m backend.tokens`
  (plik `tokens.json` 0600, tylko skroty SHA-256; odwolanie: `revoke`). Porownanie w stalym czasie, na bajtach.
- Viewer: backend odrzuca `confirm: true` i `undo` z `execute`, a narzedzia zmieniajace stan (takze zapis pamieci
  Pipe) zwracaja modelowi odmowe -- pytanie o TAK nie powstaje. Egzekwuje to backend, nie klient.
- Sesja jest przypieta do tozsamosci tokenu, ktora ja zalozyla: inny token nie odczyta jej historii ani nie
  zatwierdzi cudzej operacji, nawet znajac `session_id`.

## MCP

**Pipe jako serwer MCP.** Zewnetrzny agent dostaje narzedzia Pipe zamiast powloki: kazda komenda przechodzi przez
klasyfikator, odczyt wykonuje sie od razu, zmiana czeka na zgode administratora (Telegram/CLI) i po niej idzie przez
bezpiecznik i dziennik. Zakazane komendy sa odrzucane, pliki z sekretami nie sa wydawane, wyniki sa redagowane.
Token viewer nie tworzy zgod. Zgode moze sprawdzic tylko agent, ktory ja utworzyl; zgody zyja w pamieci i wygasaja
po 30 minutach. `ask_pipe` uruchamia agenta Pipe w roli viewer. Endpoint HTTP: tylko localhost, token Pipe
(`Authorization: Bearer`), walidacja `Origin` (403) i naglowkow 2026-07-28 (`-32020`).

**Pipe jako klient MCP.** Serwer MCP to cudzy kod: dodanie serwera zawsze wymaga potwierdzenia (widac program albo
URL; wartosci `env`/`headers` sa ukryte), `mcp.json` ma prawa 0600, a podproces stdio dostaje minimalne srodowisko
(PATH, HOME, LANG... i to, co podano) -- nigdy klucza LLM ani tokenow Pipe. Kazde wywolanie narzedzia wymaga TAK
z widocznymi argumentami, chyba ze uzytkownik oznaczyl je jako bezpieczne (`autoApprove`, `trustReadOnly`) --
adnotacje serwera same z siebie nie wystarczaja. Wyniki narzedzi MCP sa danymi dla modelu, przechodza redakcje
sekretow, a viewer nie zatwierdzi zadnego wywolania.

## Webhooki

- Serwer webhookow jest domyslnie wylaczony; bez `WEBHOOK_TOKEN` nie startuje. Nasluchuje na `127.0.0.1`
  (w Dockerze port publikowany tylko na localhost) -- nadawcy spoza serwera przez odwrotne proxy z TLS.
- Tresc alertu pochodzi z zewnatrz: trafia do modelu jako dane, a automatyczne badanie robi worker, ktory
  wykonuje wylacznie odczyty. Limit badan: raz na godzine na alert, 10 dziennie.

## Dziennik zmian i cofanie

- Kopie plikow sprzed zmian leza w `backend/data/journal/` (katalogi `0700`, pliki `0600`). Edytowany `.env`
  trafia tam w calosci -- to kopia na dysku serwera, nie wysylana do LLM. Limit: 50 wpisow, 14 dni, 5 MB na plik.
- `/cofnij` pokazuje roznice i komendy odwrotne przed TAK. Komendy odwrotne przechodza przez klasyfikator:
  zakazana komenda jest pomijana i logowana. Identyfikator wpisu jest walidowany (tylko hex), wiec nie wskaze
  sciezki poza katalogiem dziennika. Przywracanie i komendy odwrotne trafiaja do audit logu.
- `/cofnij` z klienta dziala bez LLM -- tak jak inne komendy wymaga tokenu (`AGENT_TOKEN`), a w Telegramie
  przycisk *Cofnij* zadziala tylko dla dozwolonego uzytkownika, ktory otworzyl podglad.

## Workery, cele i rutyny

- **Workery i rutyny wykonuja tylko `safe`.** Komenda `confirm` nie jest wykonywana; trafia do raportu jako
  propozycja, ktora glowny agent moze wykonac przez `remote_exec` -- z Twoim potwierdzeniem. `forbidden`
  jest logowane jak zawsze.
- **Dodanie celu i rutyny wymaga potwierdzenia** -- zmanipulowany model nie doda po cichu serwera atakujacego.
  Potwierdzenie rutyny pokazuje cala tresc zadania.
- Pola celu sa walidowane wzorcami, komenda idzie jako jeden argument (`shlex.quote`). Na celu-klastrze
  dozwolona jest jedna komenda kubectl/helm (dalsze segmenty `| grep` dzialaja lokalnie) -- druga komenda
  kubectl poszlaby do domyslnego kontekstu.
- SSH w trybie `BatchMode` (bez hasel), `StrictHostKeyChecking=accept-new`, `known_hosts` w `backend/data`.

## Czuwanie

Sprawdzenia sa deterministyczne i tylko czytaja. Przycisk *Zbadaj* wysyla agentowi wiadomosc zbudowana przez
backend (nie przez klienta) -- dalej obowiazuja zwykle zasady. Subskrypcja zdarzen wymaga tokenu, a bot
wysyla alerty tylko uzytkownikom z `TELEGRAM_ALLOWED_USER_IDS`.

---

## Workspace (narzedzia plikowe)

`read_file`, `write_file`, `change_directory` rozwiazuja sciezke (symlinki, `..`) i odrzucaja ponizsze sciezki.
W Dockerze symlink hosta (`/etc/nginx/sites-enabled/x -> /etc/nginx/sites-available/x`) jest rozwiazywany wzgledem
korzenia hosta (`/hostfs`), a `..` nie wychodzi ponad ten korzen. Odrzucane:
wszystko poza hostem (`/hostfs` w Dockerze), `/boot`, `/dev`, `/proc`, `/sys`, `/var/spool`, katalogi binarek,
`/root/.ssh`, `/home/*/.ssh`, `/etc/shadow`, `/etc/gshadow`, prywatne klucze hosta SSH. Zapis do
`/etc/passwd`, `/etc/shadow`, `/boot`, `/dev` jest zakazany; kazdy inny zapis wymaga potwierdzenia.

## Pamiec agenta

Pamiec trafia do promptu KAZDEJ przyszlej rozmowy (SERVER.md, DIRECTORY, VIBE) albo jest ladowana na zadanie
(skille), wiec jeden udany prompt injection moglby zaszczepic tam trwala instrukcje. Obrona:

- **Zapis/zastapienie skilla i pelne nadpisanie SERVER.md wymagaja potwierdzenia** (z podgladem tresci/diffem) --
  najsilniejsze wektory trwalej injekcji nie przechodza bez Twojego TAK.
- **Kazdy inny zapis pamieci jest zglaszany widocznym komunikatem `[PAMIEC]`** (Telegram i CLI) -- pamieci
  nie da sie zmienic po cichu, nawet gdy model probuje to ukryc w swojej odpowiedzi.
- kazdy zapis jest w audit logu (sciezka, bez tresci),
- oczywiste sekrety sa odrzucane, a z adresow remote repozytoriow usuwane sa dane logowania,
- w prompcie tresc jest oznaczona jako **dane, nie polecenia** -- nie zmienia klasyfikacji ani potwierdzen;
  VIBE to tylko wskazowki stylu.

Mimo to przegladaj od czasu do czasu `backend/data/` (`/server`, `/katalogi`, `/vibe`, `/skille`) -- ramowanie
"dane, nie polecenia" nie jest gwarancja.

---

## Ograniczenia, o ktorych trzeba wiedziec

- **`docker.sock` = root na hoscie.** Kto moze wykonac `docker run -v /:/x`, ma pelny dostep do hosta --
  montowanie `/` jako read-only niewiele tu zmienia. Dlatego `docker run/exec` zawsze wymaga potwierdzenia,
  ale ostatecznym zabezpieczeniem jest Twoje TAK. Jesli nie potrzebujesz zarzadzania kontenerami,
  usun montowanie `docker.sock` z `docker-compose.yml`.
- Klasyfikator nie jest sandboxem. Rozpoznaje wzorce; potwierdzenie jest po to, zebys widzial, co sie wykona.
  Czytaj komendy przed TAK.
- Tryb native i nakladka Kubernetes `host-agent` dzialaja jako root na maszynie.
- Nakladka Kubernetes `operator` pozwala usuwac pody i skalowac workloady (z potwierdzeniem).

## Audit log

```
[2026-09-24 14:23:11] [cli:kuba] [SAFE] df -h /hostfs → exit_code=0
[2026-09-24 14:25:03] [telegram:123456789] [CONFIRMED] docker restart shop-web → exit_code=0
[2026-09-24 14:26:44] [telegram:123456789] [BLOCKED] execute_command(rm -rf /) → FORBIDDEN
[2026-09-24 14:30:02] [worker:web-1@telegram:123456789] [SAFE] ssh ... web-1 -- 'uptime' → exit_code=0
```

Zawartosc plikow (read_file, write_file, pamiec) nigdy nie trafia do logu. Znaki nowej linii w komendzie sa
escapowane (`\n`), wiec komenda z `\n` nie sfalszuje kolejnych wpisow. Token porownywany jest w stalym czasie
(`hmac.compare_digest`).

## Zalecenia

1. **`AGENT_TOKEN`** -- kreator i `scripts/install-server.sh` generuja go automatycznie; w trybie kubernetes
   jest wymagany (backend nie wystartuje bez niego). Ustawiasz recznie tylko przy zmianie.
2. Nie publikuj portu 7379 na publicznym interfejsie -- dostep przez tunel SSH albo `kubectl port-forward`.
   Nakladka Kubernetes dokłada `NetworkPolicy` (deny z innych podow) obok tokenu.
3. Ogranicz `TELEGRAM_ALLOWED_USER_IDS` do minimum.
4. Dla workerow na innych serwerach uzywaj dedykowanego klucza SSH i uzytkownika z ograniczonymi uprawnieniami.
5. Czytaj komendy przed potwierdzeniem i przegladaj audit log (`/historia`) oraz pamiec (`/skille`, `/server`).
