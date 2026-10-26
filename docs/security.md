# Bezpieczenstwo -- Pipe

Pipe v0.47.0

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

Poza recznymi przypadkami klasyfikator jest fuzzowany (`backend/tests/test_security_fuzz.py`, hypothesis):
rozpoznany odczyt plus doklejona komenda (`;`, `&&`, `|`, `&`, nowa linia, `$(...)`, backticki, `<(...)`, `${IFS}`,
`$'\x3b'`), przekierowanie do pliku, nazwa komendy rozcieta cudzyslowami albo backslashem, zmiany wielkosci liter
i bialych znakow, odczyt sekretu ze sciezka w cudzyslowach. Wlasciwosc jest jedna: nic z tego nie moze byc `safe`,
a dowolny tekst nie moze wywrocic klasyfikatora. W CI 300 przykladow na test; glebiej lokalnie:
`PIPE_FUZZ_EXAMPLES=5000 pytest backend/tests/test_security_fuzz.py`. Kontrprzyklady trafiaja na liste
`KNOWN_BYPASSES` jako stale regresje.

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

## Tryb YOLO (`/yolo`)

Domyslnie kazda zmiana czeka na TAK. `/yolo on` (CLI, `pipe web`, Telegram) wylacza to pytanie -- **tylko w tej
rozmowie (sesji)**, tylko dla administratora i tylko do restartu backendu albo `/yolo off`. Co sie nie zmienia:

- klasyfikacja: operacje `forbidden` sa nadal odrzucane, a `safe` dzialaja jak zawsze;
- bezpiecznik: kazda zmiana idzie przez ten sam plan co po TAK (kopia plikow, sprawdzenie przed, weryfikacja po,
  automatyczne przywrocenie configow) i trafia do dziennika -- `/cofnij` dziala;
- workery nadal tylko czytaja (propozycje zmian nie sa wykonywane), a zgody zewnetrznych agentow MCP nadal czekaja na decyzje;
- rola viewer nie moze wlaczyc YOLO, a nawet ustawiona flaga nic nie zmienia dla sesji viewera.

Wykonanie bez pytania jest widoczne: klient dostaje `progress` „YOLO — wykonuje bez pytania: ...”, `pipe web` pokazuje
czerwony znacznik nad polem wpisywania, CLI -- `YOLO` w prompcie, a audit log i dziennik zapisuja interfejs z dopiskiem
`[yolo]` (np. `cli:kuba [yolo]`). Model dostaje w prompcie blok o trybie YOLO (ostroznosc, najmniejsza zmiana,
bez nieodwracalnych operacji bez wyraznej prosby). YOLO usuwa czlowieka z petli -- wlaczaj je na czas konkretnej pracy.

## Tryb obserwacji (`PIPE_OBSERVE=1`)

Na poczatek, zanim zaufasz agentowi, mozesz zainstalowac go tak, zeby niczego nie zmienial:
`sudo bash scripts/install-server.sh --profile observe`. Pipe dalej pilnuje serwera, wysyla alerty i raporty,
odpowiada na pytania, rysuje schematy i dziala jako bramka MCP do odczytu. Zmiany sa odrzucane na dwoch poziomach.

W aplikacji (`runtime.observe()`, takze w trybie native):

- komenda zmieniajaca stan, zapis pliku, `journal undo`, `cron_manage add/remove` i `pipe_update apply`
  sa odrzucane bez pytania o TAK, rowniez w trybie YOLO; model dostaje w prompcie blok o trybie obserwacji,
- `run_command` w bramce MCP odmawia zamiast tworzyc zgode, a juz istniejacej zgody nie da sie zatwierdzic,
- `undo` z `execute` (przycisk w `pipe web`, `/cofnij` w CLI i Telegramie) zwraca blad,
- rejestry i pamiec Pipe dzialaja dalej z potwierdzeniem jak zwykle: rutyny (czytaja), cele, SERVER.md, skille.

W kontenerze (`backend/docker-compose.observe.yml`, tylko tryb docker):

- agent nie ma `docker.sock`; Dockera widzi przez `tecnativa/docker-socket-proxy` z API tylko do odczytu
  (`ps`, `inspect`, `logs`, `info` dzialaja; `run`, `exec`, `restart`, `stop` dostaja 403). Proxy jest w osobnej
  sieci wewnetrznej, bez dostepu z zewnatrz,
- `/root` hosta jest tylko do odczytu (bez nakladki rw),
- kontener nie ma uprawnien jadra poza `DAC_READ_SEARCH` (czytanie plikow) i `NET_RAW` (ping) i dziala
  z `no-new-privileges`; `/proc/*/environ` procesow hosta jest wtedy nieczytelny.

Zapisywalne zostaja `backend/data` (pamiec Pipe) i `/tmp` hosta (socket dla bota Telegram). W trybie native
obowiazuje tylko ochrona w aplikacji: proces dziala jako root. Pelny tryb przywraca
`sudo bash scripts/install-server.sh --profile full`.

## Internet (`web_search`, `web_fetch`, `software_info`)

Agent sam decyduje, kiedy siegnac do sieci, wiec internet jest traktowany jak niezaufane zrodlo:

- **Nic poufnego nie wychodzi**: zapytanie, adres i argumenty `software_info` z sekretem (`memory.find_secret()`)
  sa odrzucane, prompt kaze pisac zapytania ogolnie (tresc bledu, oprogramowanie i wersja -- bez domen, IP, nazw
  uzytkownikow). Kazde zapytanie trafia do audit logu.
- **Adres to tez kanal wycieku** (`https://obcy.example/?d=<dane>`): `web_fetch` czyta bez pytania tylko adresy
  z wynikow `web_search` tej sesji (`Session.web_urls`) albo wpisane przez uzytkownika (wiadomosci zbudowane przez
  backend, np. alerty z webhookow, sie nie licza). Inny adres -- potwierdzenie.
- **Tylko publiczny internet** (SSRF): host musi wskazywac wylacznie na adresy publiczne (`ipaddress.is_global`);
  loopback, sieci prywatne, link-local (metadane chmury `169.254.169.254`), CGNAT i adresy IPv4 w IPv6 sa
  odrzucane -- przy kazdym przekierowaniu osobno. Ograniczenie: sprawdzenie DNS i polaczenie to dwa kroki
  (teoretycznie mozliwy DNS rebinding); lokalne uslugi agent sprawdza przez `network_info`.
- **Prompt injection**: wyniki sa oznaczone jako dane, nie polecenia. Strone z pytaniem czyta osobne zapytanie
  do modelu **bez narzedzi** (`WEB_READER_PROMPT`) -- do agenta trafia tylko odpowiedz. Po przeczytaniu czegokolwiek
  z internetu (`Session.web_tainted`) tryb YOLO jest wstrzymany do nastepnej wiadomosci uzytkownika: zmiana
  zaproponowana pod wplywem strony zawsze czeka na TAK.
- Wyszukiwarki bez klucza to nieoficjalne endpointy (DuckDuckGo) i publiczne API (Stack Exchange, Wikipedia,
  endoflife.date, OSV.dev); `WEB_SEARCH=off` zabiera agentowi internet calkowicie.
- **Skan podatnosci** (czuwanie, bez agenta) wysyla do OSV.dev nazwy i wersje pakietow systemu i pobiera katalog
  CISA KEV; nic wiecej nie opuszcza serwera. Wylacza go `VULN_INTERVAL=0` (niezaleznie od `WEB_SEARCH`). Komende
  latki sklada serwer z nazw pasujacych do wzorca nazwy pakietu; klient przysyla tylko token z podgladu.

## Zalaczniki (pliki, zrzuty ekranu, glosowki)

Plik wyslany przez uzytkownika to tez obcy tekst -- log moze zawierac linie napisana przez atakujacego:

- tresc pliku jest opisana w wiadomosci jako DANE, nie polecenia, a po wiadomosci z zalacznikiem tryb YOLO jest
  wstrzymany do nastepnej wiadomosci (`Session.web_tainted`) -- zmiana zaproponowana pod wplywem pliku czeka na TAK,
- pliki tekstowe przechodza przez `memory.redact_secrets()` przed wyslaniem do modelu (jak wyniki narzedzi),
- tekst z zalacznika nie uczy VIBE i nie liczy sie jako adres *wpisany przez uzytkownika* dla `web_fetch`
  (`pipe_text` trzyma tylko to, co uzytkownik napisal),
- obraz jest w historii tylko w swojej turze; pliki binarne leza w `DATA_DIR/uploads/` z prawami 0600 przez 7 dni,
  zapis trafia do audit logu; viewer nie moze zapisac pliku na serwerze (obrazy i tekst -- tak),
- rodzaj pliku wynika z zawartosci (sygnatura, UTF-8), nie z nazwy ani typu MIME od klienta; nazwa jest
  sprowadzana do bezpiecznych znakow (bez katalogow),
- limity: 10 MB na plik, 20 MB na wiadomosc; `READ_LIMIT` serwera i `MAX_BODY` mostu `pipe web` (32 MiB)
  wynikaja z nich.

## Role i tokeny

- `AGENT_TOKEN` -- admin, `AGENT_VIEWER_TOKEN` -- viewer, tokeny klientow z `python3 -m backend.tokens`
  (plik `tokens.json` 0600, tylko skroty SHA-256; odwolanie: `revoke`). Porownanie w stalym czasie, na bajtach.
- Viewer: backend odrzuca `confirm: true` i `undo` z `execute`, a narzedzia zmieniajace stan (takze zapis pamieci
  Pipe) zwracaja modelowi odmowe -- pytanie o TAK nie powstaje. Egzekwuje to backend, nie klient.
- Sesja jest przypieta do tozsamosci tokenu, ktora ja zalozyla: inny token nie odczyta jej historii ani nie
  zatwierdzi cudzej operacji, nawet znajac `session_id`.

## Pipe w internecie (`/web`)

`/web wlacz` wystawia `pipe web` pod publicznym adresem HTTPS (`core/webpublic.py`, `core/webaccess.py`):

- mikr.us (`/web wlacz mikrus` albo sam, gdy na hoscie jest `/klucz_api`): Caddy po zwyklym HTTP tylko na IPv6
  (`[::]:7443`), HTTPS konczy proxy mikr.us. Odcinek proxy -> serwer idzie bez TLS przez siec mikr.us; ciasteczko
  urzadzenia ma `Secure`, wiec przegladarka wysyla je tylko po HTTPS. Adres klienta z `X-Forwarded-For` proxy --
  da sie go podrobic, laczac sie z portem wprost, ale sluzy tylko do pokazania i limitu prob (globalny limit
  oczekujacych prosb zostaje).
- Tunel (`/web wlacz tunel` albo sam, gdy porty 80/443 sa nieosiagalne): kontener `cloudflared` (przypieta wersja)
  bez portow, plikow i uprawnien (`--cap-drop ALL`, `no-new-privileges`), we wspolnej sieci z kontenerem Pipe --
  most slucha dla niego tylko na `127.0.0.1:7402`, niewidocznym z zewnatrz. Cloudflare konczy TLS, wiec widzi ruch strony (tresc rozmowy, ciasteczko
  urzadzenia) -- to swiadomy kompromis za dzialanie bez otwartych portow; kto tego nie chce, uzywa
  `/web wlacz bezposrednio` z wlasna domena albo tunelu SSH. Adres klienta z naglowka `Cf-Connecting-Ip` (ustawia go
  Cloudflare). Nowy adres tunelu po restarcie = nowa domena, wiec ciasteczka starych urzadzen nie pasuja i trzeba je
  wpuscic jeszcze raz.

- Caddy (`caddy:2.11-alpine`, kontener `pipe-web`) ma siec hosta, ale tylko gniazdo mostu (`/tmp/pipe-web/web.sock`)
  i wolumen certyfikatow -- bez `docker.sock`, `--cap-drop ALL` (+ `NET_BIND_SERVICE`), `no-new-privileges`,
  limity pamieci i procesow, bez API administracyjnego. Przekazuje adres klienta naglowkiem, ktorego klient nie
  podrobi (Caddy go nadpisuje).
- Most w procesie Pipe nie ma tokenu backendu. Bez ciasteczka urzadzenia odpowiada tylko strona prosby o dostep,
  jej pliki i dwa wywolania parowania; reszta to 401. Host musi byc adresem strony, Origin -- `https://` tego adresu.
- Parowanie: prosba dostaje kod (6 znakow, bez mylacych sie liter) i sekret znany tylko stronie, ktora ja wyslala.
  Zgode daje administrator z rozmowy (przycisk z backendu albo `/web wpusc <kod>`); kod na ekranie i w wiadomosci
  pozwala sprawdzic, ze zatwierdzasz swoje urzadzenie. Prosba wygasa po 5 minutach, najwyzej 5 prosb na 10 minut
  z jednego IP i 10 oczekujacych naraz. Token urzadzenia trafia raz do strony, ktora poprosila, i tylko jako
  ciasteczko `HttpOnly; Secure; SameSite=Lax` (Lax, zeby link z Telegrama otwieral sie zalogowany; API to POST
  z kontrola Origin). Zatwierdzony, a nieodebrany w 2 minuty token jest usuwany.
- Urzadzenie to zwykly token klienta w `tokens.json` (nazwa `web-...`, tylko skrot SHA-256, rola admin albo viewer),
  wiec uprawnienia egzekwuje backend jak dla kazdego innego tokenu. Wygasa po 30 dniach bez uzycia; `/web odwolaj`
  dziala od razu, takze dla otwartej karty (strumien zdarzen sprawdza urzadzenie co 25 s). Tokeny urzadzen nie
  zamykaja trybu bez tokenu -- CLI i Telegram bez `AGENT_TOKEN` dzialaja dalej, a strona i tak wymaga urzadzenia.
- Wlaczenie, wylaczenie, wpuszczanie i odwolywanie -- tylko administrator; kazda decyzja trafia do audit logu
  z kodem, urzadzeniem i IP. Tryb obserwacji odmawia `/web wlacz`.

Strona w internecie to nowa droga do Pipe, ktory ma `docker.sock`. Wpuszczaj tylko urzadzenia, ktorych kod widzisz
na wlasnym ekranie, a nieuzywane odwoluj. Do samego czytania wystarczy *Tylko podglad*.

## MCP

**Pipe jako serwer MCP.** Zewnetrzny agent dostaje narzedzia Pipe zamiast powloki: kazda komenda przechodzi przez
klasyfikator, odczyt wykonuje sie od razu, zmiana czeka na zgode administratora (Telegram/CLI) i po niej idzie przez
bezpiecznik i dziennik. Zakazane komendy sa odrzucane, pliki z sekretami nie sa wydawane, wyniki sa redagowane.
Token viewer nie tworzy zgod. Zgode moze sprawdzic tylko agent, ktory ja utworzyl; zgody zyja w pamieci i wygasaja
po 30 minutach. `ask_pipe` uruchamia agenta Pipe w roli viewer. Endpoint HTTP: tylko localhost, token Pipe
(`Authorization: Bearer`), walidacja `Origin` (403) i naglowkow 2026-07-28 (`-32020`).

**Pipe jako klient MCP.** W v0.35 klient zostal usuniety: cudzy serwer dzialal jako proces Pipe, z jego dostepem do
`docker.sock` i plikow. Od v0.42 wraca z inna granica: serwer stdio (`npx`, `uvx`, obraz Dockera) dziala w jednorazowym
kontenerze bez montowan, `docker.sock` i uprawnien (`--cap-drop ALL`, `no-new-privileges`, uzytkownik `nobody`, limity),
wiec moze najwyzej korzystac z sieci i zmieniac swoja usluge (np. repozytorium na GitHubie) -- nie serwer. Inne
programy niz `npx`/`uvx`/obraz i opcje `docker run` typu `-v`, `--network host`, `--privileged` sa odrzucane. Sekrety
tylko jako `${MCP_*}` z `backend/.env` albo z `mcp_secrets.json` (0600; wpisywane w oknie `/mcp` w `pipe web` i CLI
komenda `mcp_servers` `klucz`, tylko administrator, wartosc nigdy nie wraca do klienta i nie trafia do rozmowy ani
audit logu -- tylko nazwa) -- model nie wskaze `${LLM_API_KEY}` ani tokenu Pipe; tekst wygladajacy na sekret
w konfiguracji -- odmowa. Dodatki z katalogu (`backend/addons/`) przechodza ta sama walidacje; `state: true` daje
serwerowi nazwany wolumen `pipe-mcp-state-<nazwa>` na `/state` (nigdy katalog hosta; usuwany razem z serwerem),
a `autoApprove` w katalogu obejmuje tylko logowanie i stan logowania. Dodanie serwera: jedno TAK z podgladem (albo `/mcp dodaj` administratora). Narzedzie bez
`readOnlyHint` -- TAK przy kazdym wywolaniu; adnotacje sa deklaracja serwera, wiec odczyty bez pytania sa bezpieczne
tylko o tyle, o ile ufasz temu serwerowi (`askAlways: true` w konfiguracji wylacza to dla niego). Wynik traktowany jak
tresc z internetu (YOLO wylaczone do nastepnej wiadomosci), argument z sekretem nie wychodzi. Tryb obserwacji i viewer:
tylko odczyty. Kazde wywolanie jest w audit logu.

## Webhooki

- Serwer webhookow jest domyslnie wylaczony; bez `WEBHOOK_TOKEN` nie startuje. Nasluchuje na `127.0.0.1`
  (w Dockerze port publikowany tylko na localhost) -- nadawcy spoza serwera przez odwrotne proxy z TLS.
- Tresc alertu pochodzi z zewnatrz: trafia do modelu jako dane, a automatyczne badanie robi worker, ktory
  wykonuje wylacznie odczyty. Limit badan: raz na 6 h na alert, 10 dziennie. *Napraw* wysyla ustalenia
  agentowi jako zwykla wiadomosc -- zmiany i tak przechodza przez klasyfikator i potwierdzenie.

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

## Warsztat (`project`) i Pipe piszacy pierwszy

Warsztat (`WORKSPACE_DIR`, domyslnie `/root/pipe-workspace`) to jedyne miejsce, w ktorym Pipe zmienia cos bez
pytania. Granice:

- **Zapis plikow bez potwierdzenia** tylko wtedy, gdy podana sciezka i sciezka po rozwiazaniu symlinkow leza w
  warsztacie i nie jest to plik startowy (`.bashrc`, cron, systemd...) -- link w sklonowanym repozytorium nie
  wyprowadzi zapisu poza warsztat. Kazdy taki zapis jest w audit logu z dopiskiem `[warsztat]`.
- **Kod z repozytorium nigdy nie dziala w procesie ani kontenerze Pipe** (tam jest `docker.sock`). `exec` uruchamia
  go w jednorazowym kontenerze z oficjalnego obrazu jezyka: tylko katalog projektu jako `/work`, bez `docker.sock`,
  `--cap-drop ALL`, `no-new-privileges`, limit pamieci, procesow i czasu. Siec ma (instalacja zaleznosci).
- **`clone`**: tylko `https://` albo `git@`, `--depth 1`, bez hookow, submodulow i LFS, bez pytania o haslo; za duze
  repozytorium (`PROJECT_MAX_MB`) jest od razu usuwane.
- **README i AGENTS.md to dane.** Po ich przeczytaniu (albo po tresci z internetu) `exec` znow wymaga potwierdzenia
  do nastepnej wiadomosci uzytkownika -- jak YOLO, tekst z cudzego repozytorium nie uruchomi sam kodu.
- **`run` i `remove` zawsze z potwierdzeniem** z planem. Port tylko na `127.0.0.1`. Compose z `privileged`,
  `docker.sock`, siecia hosta, montowaniem katalogow systemowych albo portem na swiat -- potwierdzenie mowi to wprost.
  `run` jest w dzienniku, `/cofnij` usuwa wdrozenie.
- `WORKSPACE_AUTONOMY=0` -- zgoda na kazda operacje. Tryb obserwacji i viewer -- tylko lista i logi.

Wiadomosci, ktore Pipe wysyla sam (skrzynka, `core/feed.py`), maja przyciski zbudowane przez backend; klient odsyla
tylko numer wiadomosci i akcji (`feed_act`), wiec nie da sie nim podac wlasnej komendy. Akcje zmieniajace serwer
(*Zwolnij miejsce*, *Zalataj*, *Napraw*) sa tylko dla administratora. *Napraw* wysyla agentowi propozycje z
samodzielnego badania jako zwykla wiadomosc -- zmiany przechodza przez klasyfikator i potwierdzenie jak zawsze.
Samodzielne badanie to worker tylko z odczytami, z limitem `AUTO_INVESTIGATE_DAILY` dziennie.

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

Skille z cudzych repozytoriow (`/pobierz`, `skill_manage install`) to ten sam wektor: przed instalacja widac podglad
(nazwy, opisy, zrodlo, co pominiete) i trzeba kliknac *Zainstaluj* albo dac TAK; do pamieci trafia sam tekst
`SKILL.md` z dopisanym zrodlem, skrypty i pliki pluginu nie (haki, komendy, agenty i programy -- nigdy). Repozytorium
jest klonowane jak w warsztacie (bez hookow, submodulow, LFS, limit rozmiaru) i usuwane po odczycie; tresc to dane --
YOLO jest wylaczone do nastepnej wiadomosci. Instaluj tylko z zaufanych zrodel.

Mimo to przegladaj od czasu do czasu `backend/data/` (`/server`, `/katalogi`, `/vibe`, `/skille`) -- ramowanie
"dane, nie polecenia" nie jest gwarancja.

---

## Ograniczenia, o ktorych trzeba wiedziec

- **`docker.sock` = root na hoscie.** Kto moze wykonac `docker run -v /:/x`, ma pelny dostep do hosta --
  montowanie `/` jako read-only niewiele tu zmienia. Dlatego `docker run/exec` zawsze wymaga potwierdzenia,
  ale ostatecznym zabezpieczeniem jest Twoje TAK.
- **Komendy na hoscie (tryb docker).** System plikow hosta jest w kontenerze tylko do odczytu, wiec zmiany na
  serwerze (`apt-get`, `systemctl`, `sed -i` w `/etc`, `ufw`, latki z `/zalataj`) Pipe wykonuje przez krotki
  kontener pomocniczy z wlasnego obrazu: `docker run --rm --privileged --pid=host`, a w nim `nsenter -t 1` do
  przestrzeni nazw hosta -- komenda dziala jak w powloce roota na serwerze. To nie jest nowe uprawnienie (patrz
  punkt wyzej), tylko jedna przewidywalna droga: model prosi o nia przez `execute_command` z `host: true`,
  klasyfikator ocenia komende docelowa, zakazane sa odrzucane, a kazda (takze odczyt) czeka na Twoje TAK.
  W potwierdzeniu widac komende docelowa i linie *na hoscie: kontener pomocniczy...*; opakowanie sklada kod,
  komenda trafia do niego jako jeden argument. Tryb obserwacji i rola viewer tej drogi nie maja. Kopii plikow
  i automatycznego przywracania tu nie ma (bezpiecznik zapisuje wpis w dzienniku i sprawdza strony). Jesli nie potrzebujesz zarzadzania kontenerami,
  zainstaluj Pipe z `--profile observe` (patrz wyzej) albo usun montowanie `docker.sock` z `docker-compose.yml`.
- **`/zalataj` nie pyta o kazda komende.** Wpisanie `/zalataj` (albo przycisk *Zalataj*) przez administratora jest
  zgoda na cala latke: Pipe od razu instaluje poprawki pakietow tego serwera, podbija zaleznosci aplikacji (menedzer
  pakietow w jednorazowym kontenerze jak `exec` w warsztacie: tylko katalog aplikacji, bez `docker.sock` i uprawnien,
  bez skryptow instalacyjnych pakietow; nazwy i wersje z lockfile'i przechodza przez wzorce ekosystemu; kopia plikow w
  dzienniku), odswieza obrazy kontenerow z compose (w Dockerfile zmienia tylko tag w linii `FROM`, w tej samej linii
  wersji, z kopia pliku w dzienniku),
  lata zdalne serwery (cele ssh: jako root albo przez `sudo -n`) i sprzata po sobie (obrazy bez tagu, cache
  budowania starszy niz doba, pobrane paczki, cache npm/pip/yarn/composer, stare jadra, ktore apt sam uznaje za
  zbedne). Komendy sklada serwer z wyniku skanu, bez modelu: nazwy pakietow i projektow compose przechodza przez
  wzorce, wiec tresc z bazy pakietow ani z etykiet kontenera nie doklei nic do powloki. Agent w rozmowie (narzedzie
  `security_patch`) dostaje te sama latke za jednym Twoim TAK (w YOLO bez pytania). Viewer i tryb obserwacji jej
  nie maja. Obrazu, ktory nie ma nowszej wersji, Pipe nie zostawia: instaluje w nim poprawki z repozytorium jego
  systemu (kontener tymczasowy z tego obrazu, `docker commit` pod tym samym tagiem). Poprzedni obraz zostaje pod tagiem
  `pipe-rollback-*`, a `/cofnij` do niego wraca.
- Rozmowy leza na dysku w `DATA_DIR/sessions` (prawa 0600, katalog 0700) przez `SESSION_KEEP_DAYS` dni, zeby
  przetrwaly restart. Wyniki narzedzi sa w nich zredagowane jak w zapytaniach do modelu, ale to, co sam wpiszesz,
  zostaje. `SESSION_KEEP_DAYS=0` wylacza zapis i przy starcie usuwa zapisane sesje.
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

## Klucze providerow LLM dodane z interfejsu

`pipe web` pozwala dodac klucz API kolejnego providera. Klucz idzie z przegladarki do lokalnego mostu (`127.0.0.1`),
tunelem SSH do backendu i tam zostaje w `DATA_DIR/llm_keys.json` (0600). Nie wraca do klienta (lista providerow podaje
tylko, czy klucz jest i skad), nie trafia do audit logu (logowana jest sama zmiana providera) ani do modelu. Zanim
zostanie zapisany, backend sprawdza go u providera -- wylacznie pod adresem z presetu albo z pliku providerow, wiec
klucza nie da sie wyslac pod dowolny adres. Zmiany moze robic tylko administrator. Odczyt `llm_keys.json` przez
agenta jest klasyfikowany jak `.env` (wymaga potwierdzenia), a redakcja sekretow obejmuje wyniki narzedzi jak zwykle.

## Aktualizacja samego Pipe (`pipe_update`)

To narzedzie pobiera kod z sieci i uruchamia go z uprawnieniami agenta, wiec jest celowo waskie:

- nie przyjmuje adresu, galezi, komendy ani sciezki -- model moze tylko wybrac `check` albo `apply`;
- zrodlem jest wylacznie `origin` repozytorium, w ktorym Pipe jest zainstalowany, przez `git pull --ff-only`
  (bez nadpisywania historii i bez scalania);
- `apply` zawsze wymaga potwierdzenia, a rola viewer jest odrzucana z gory;
- projekt, katalog i uslugi compose pochodza z etykiet Dockera wlasnego kontenera i sa cytowane pojedynczo;
- naraz dziala jedna aktualizacja; kontener pomocniczy dostaje `docker.sock` i katalog repozytorium, nic wiecej.

Zaufanie przenosi sie na zdalne repozytorium: kto moze wypchnac commit na sledzona galaz, ten po Twoim "TAK"
uruchomi kod na serwerze. To ta sama granica co przy recznym `git pull`, ale warto o niej pamietac.
