# Bezpieczeństwo -- Pipe

Pipe v0.50.0

## Model

Pipe daje modelowi językowemu dostęp do serwera, więc zasady bezpieczeństwa są **w kodzie**, a nie
w prompcie. Model może się pomylić albo zostać zmanipulowany treści przeczytanego pliku (prompt injection);
klasyfikator, potwierdzenia, redakcja sekretów i limity workerów działają niezależnie od tego, co model
"chce" zrobić.

| Warstwa | Co robi |
|---------|---------|
| Klasyfikator komend | `safe` / `confirm` / `forbidden` -- fail-closed |
| Samodzielność | rutynę wykonuje sam przez bezpiecznik, o sprawy ważne pyta (`core/autonomy.py`) |
| Potwierdzenie | pokazuje dokładnie ten napis, który zostanie wykonany |
| Redakcja sekretów | wyniki narzędzi są czyszczone z kluczy i haseł przed wysłaniem do LLM |
| Workery i rutyny | wykonują wyłącznie `safe`; reszta wraca jako propozycja |
| Workspace | narzędzia plikowe widzą tylko hosta i nie widzą kluczy SSH, `/etc/shadow`, `/proc`... |
| Audit log | każda operacja, bez treści plików |
| Token | `AGENT_TOKEN` wymagany dla każdego żądania, także potwierdzeń i subskrypcji |

---

## Klasyfikacja komend (`backend/core/security.py`)

Kolejność:

1. **FORBIDDEN** na całym napisie -- łapie `curl ... | bash` i `$(rm -rf ~)`.
2. Podział na segmenty po `;`, `|`, `||`, `&&`, `&` i nowych liniach -- **z uwzględnieniem cudzysłowów**
   (`grep 'a|b'` to jeden segment).
3. Każdy segment: forbidden -> wrażliwe pliki (confirm) -> wzorce confirm -> rozpoznany odczyt (safe).
   Dopasowanie jest **tokenowe**: `ss` nie pasuje do `ssh`, `ps` do `psql`, `id` do `idiotic-cmd`.
4. Przekierowanie zapisu (`>`, `>>` poza `/dev/null` i `2>&1`) albo dynamiczna konstrukcja (`$(...)`,
   `${...}`, `$'...'`, backtick, `<(...)`) -> **confirm**, nawet gdy segmenty są odczytami.
5. Wszystko, czego klasyfikator nie rozpoznaje -> **confirm** (fail-closed).

### SAFE -- rozpoznane odczyty

Pliki i tekst (`cat`, `grep`, `ls`, `find`, `du`, `df`, `jq`...), system (`ps`, `free`, `uptime`,
`systemctl status`, `journalctl`), sieć (`ss`, `ip addr`, `ping`, `dig`, `curl` GET), Docker
(`ps`, `logs`, `inspect`, `stats`, `compose ps/logs/config/top`, `exec` z rozpoznanym odczytem w środku -- także
`sh -c '...'`), Git (`status`, `log`, `diff`, `show`, listowanie galezi/tagow), Kubernetes (`get`, `describe`, `logs`,
`top`, `rollout status`), Helm (`list`, `status`, `get`), `dmesg` (bez czyszczenia), `sed` bez `-i` i bez komend
`e`/`w`, `awk` bez `system()`, potoków, `getline`, `print > plik` i `ENVIRON`.

Argumenty, które zmieniają stan, są sprawdzane osobno:

| Komenda | Wymaga potwierdzenia, gdy... |
|---------|------------------------------|
| `git` | podkomenda nie jest odczytem, flaga uruchamiająca program (`-c core.pager=`, `--upload-pack`, `--receive-pack`, `--exec`, `-O`/`--open-files-in-pager`) albo pisząca do pliku (`-o`/`--output`), `branch -D`, `branch nowa`, `tag v1`, `remote add`, `config` bez `--get/--list` |
| `find` | `-exec`, `-execdir`, `-ok`, `-delete`, `-fprint` |
| `curl` | `-o/-O`, `-T`, `-d/--data`, `-F`, `--json`, `-D`, `--trace*`, `-X`/`--request` (także `--request=`, `-X<METODA>`) inne niż GET/HEAD, **albo host inny niż localhost/prywatny** (eksfiltracja) -- także proxy (`-x`, `--proxy`, `--socks5`), `--resolve`/`--connect-to` i adres zapisany jedna liczba (`http://134744072/`) |
| `sort` | `-o`/`--output`, `--compress-program` (uruchamia program) |
| `journalctl` | `--vacuum-*`, `--rotate`, `--flush` |
| `kubectl get secret` | `-o yaml/json/jsonpath/go-template/custom-columns` (także `-o=...`) albo `--template` -- również gdy sekrety są na liście zasobów (`get cm,secret`) -- treść sekretu trafiłaby do LLM |

Odczyty tych ścieżek też wymagają zgody, bo wysłałyby sekrety do providera:
`/etc/shadow`, klucze SSH, `*.pem`/`*.key`, `.env` (także względne `cat .env`), **`/proc/*/environ` i `/proc/*/root`**
(dowolny PID, `self` i globy; przy `pid: host` to zmienne i katalogi domowe procesów hosta -- obejście
read-only `/hostfs`), oraz globy w katalogach z sekretami (`cat /etc/s*adow`, `cat /root/.ssh/id_*`).
Wzorce są sprawdzane także po usunięciu cudzysłowów i backslashy, więc `cat /etc/sha""dow` nie przejdzie.
Te same pliki czytane przez `read_file` też wymagają zgody.

### CONFIRM -- zmiany stanu

Nie każda z nich czeka na Ciebie -- rutynę Pipe wykonuje sam (sekcja *Samodzielność* niżej). Do potwierdzenia
klasyfikator kieruje: `rm`, `mv`, `cp`, `chmod`, `chown`, `kill`, `systemctl start/stop/restart/enable...`, `apt install/upgrade/remove`,
`docker run/exec/start/stop/restart/rm/compose up/down`, `git push/commit/pull/reset...`, `kubectl apply/delete/scale/exec...`,
`helm install/upgrade/uninstall`, `ufw`, `iptables` (poza listowaniem), `reboot`, `crontab` (poza `-l`) --
oraz każda nieznana komenda. Odczyt plików z sekretami (`/etc/shadow`, klucze SSH, `*.pem`, `*.key`, `.env`)
też wymaga zgody.

### FORBIDDEN -- zawsze odrzucane

`rm -rf /` (i warianty: `-fr`, `/*`, `~`, `"/"`, `/hostfs`), `rm --no-preserve-root`, `dd if=`, `mkfs`,
`wipefs`, `shred`, zapis do `/etc/passwd`, `/etc/shadow`, `/boot/`, `/dev/sd*`, fork bomb,
`curl|wget ... | sh`, `base64 ... | sh`, `python -c ...exec`, `kubectl delete ns kube-system`.
Odmowa nie wraca do modelu jako błąd do ponowienia -- dostaje informacje, żeby nie próbował obejść.

Regresje obejść znalezionych przy przebudowie: `backend/tests/test_security_hardening.py`,
`backend/tests/test_review_fixes_v091.py`.

Poza ręcznymi przypadkami klasyfikator jest fuzzowany (`backend/tests/test_security_fuzz.py`, hypothesis):
rozpoznany odczyt plus doklejona komenda (`;`, `&&`, `|`, `&`, nowa linia, `$(...)`, backticki, `<(...)`, `${IFS}`,
`$'\x3b'`), przekierowanie do pliku, nazwa komendy rozcięta cudzysłowami albo backslashem, zmiany wielkości liter
i białych znaków, odczyt sekretu ze ścieżką w cudzysłowach. Właściwość jest jedna: nic z tego nie może być `safe`,
a dowolny tekst nie może wywrócić klasyfikatora. W CI 300 przykładów na test; głębiej lokalnie:
`PIPE_FUZZ_EXAMPLES=5000 pytest backend/tests/test_security_fuzz.py`. Kontrprzykłady trafiają na listę
`KNOWN_BYPASSES` jako stałe regresje.

---

## Redakcja sekretów

Każdy wynik narzędzia (komendy, plik, raport workera) przechodzi przez `memory.redact_secrets()` przed
wysłaniem do providera LLM:

- klucze prywatne (cały blok PEM), klucze AWS, `sk-...`, tokeny GitHub, Slack, Google, botów Telegrama, **JWT**,
- `NAZWA_Z_PASSWORD/SECRET/TOKEN/API_KEY=wartosc` -- klucz zostaje, wartość znika
  (`DB_PASSWORD=[ZREDAGOWANO: sekret]`), więc agent wie, że zmienna istnieje. Dopasowanie nie przechodzi
  przez znaki NUL, więc nie zjada całego `/proc/<pid>/environ`,
- **dane logowania w URL-ach** (`postgres://user:haslo@host` -> `postgres://user:[ZREDAGOWANO]@host`), w tym
  `git remote -v` i `docker inspect`,
- **hashe haseł z `/etc/shadow`** (`$6$...`).

Agent nie może zapisać `write_file` treści ze znacznikiem `[ZREDAGOWANO` -- inaczej przepisując plik `.env`
zniszczyłby prawdziwe wartości. Musi zmieniać taki plik punktowo (np. `sed -i`, z potwierdzeniem).
Wyłączenie: `REDACT_SECRETS=0` (niezalecane).

Wyniki dłuższe niż 24 000 znaków są przycinane (początek + koniec), a historia sesji -- na granicy wiadomości
użytkownika, żeby para wywolanie/wynik narzędzia nigdy nie została rozdzielona.

---

## Potwierdzenia: co widzisz, to wykonasz

- Komenda w potwierdzeniu (`as_code()`) ma **zneutralizowane znaki sterujące i bidi** -- sekwencje ANSI, `\r`
  i przełączniki kierunku pisma (U+202E) są zamieniane na widoczny zapis `\xNN`, więc nie ukryją różnicy
  między tym, co widzisz, a tym, co się wykona.
- `write_file` pokazuje **diff względem obecnej treści** (albo podgląd nowego pliku), a przy plikach
  uruchamianych automatycznie (`.bashrc`, `authorized_keys`, cron, systemd, `.gitconfig`) dodaje ostrzeżenie
  o trwałym dostępie.
- Potwierdzenie jest przypięte do `tool_call_id` w sesji; nowa wiadomość zamiast TAK anuluje operację.
- Plan bezpiecznika (kopie, sprawdzenia, weryfikacja) jest częścią potwierdzenia i jest przechowywany w nim --
  po TAK wykonywany jest dokładnie pokazany plan. Komendy sprawdzające (`nginx -t`, `sshd -t`, `docker inspect`...)
  są składane z szablonów w kodzie, parametry przez `shlex.quote`.

## Samodzielność: co Pipe robi bez pytania

Operacja, którą klasyfikator zostawia do potwierdzenia, przechodzi jeszcze przez `backend/core/autonomy.py`, zanim
trafi do Ciebie. Rutynę Pipe wykonuje od razu, przez ten sam bezpiecznik co po TAK: kopia plików do dziennika,
sprawdzenie przed (`nginx -t`, `sshd -t`, `docker compose config`), weryfikacja po (usługa aktywna, kontener
działa, strony odpowiadają), automatyczne przywrócenie configu, wpis, który `/cofnij` odwraca. Rutyna to:

- restart, przeładowanie, start, `enable` usług i kontenerów, `nginx -s reload`, `docker compose up -d/pull/build`,
  `docker compose down` (bez `-v`), `docker image prune` (bez `-a`), `docker pull`, `kubectl rollout restart/undo`,
  `kubectl scale`, `helm rollback`,
- edycja plików konfiguracji (`write_file`, `sed -i`, `tee`, `cp`, `mv`, `ln -s`, `> plik`), `mkdir`, `chmod`/`chown`
  bez setuid i bez zapisu dla wszystkich, `rm` pojedynczych plików, które dziennik skopiuje; konfiguracja nginx,
  Apache i Caddy też, o ile zmiana nie dodaje `root`, `alias` ani `autoindex`, a treść stoi w samej komendzie
  albo pochodzi z tego samego katalogu serwera,
- `apt`/`dnf`/`apk` install/upgrade/update/clean z repozytorium systemu, `dpkg --configure -a`, `journalctl --vacuum`,
  `certbot renew`, `git pull/fetch` z nazwanego remote (`origin`), `git checkout <galaz>/commit/merge/stash`,
- `curl` GET do domen, które ten serwer sam obsługuje (z konfiguracji proxy),
- łatki (`security_patch`), cofnięcie zmiany (`journal undo`), zadanie jednorazowe (worker tylko czyta),
  aktualizacja Pipe, uruchomienie projektu z warsztatu.

O sprawy ważne Pipe pyta zawsze, z powodem w kodzie:

| Powód | Przykłady |
|-------|-----------|
| dane, których nie skopiuje | `rm -r`, globy, pliki większe niż kopia dziennika (5 MB), `docker rm`, wolumeny, `system prune`, `compose down -v`, `apt remove`, `git reset --hard`, `git clean`, `git checkout -- .`, `kubectl delete` |
| dostęp do serwera | SSH (`/etc/ssh`, `.ssh/`), użytkownicy i hasła, sudo, PAM, zapora (`ufw`, `iptables`, `nft`), setuid, `chmod 777` |
| autostart | cron, jednostki systemd, `.bashrc`/`.profile`, `/etc/profile.d`, `rc.local`, `logrotate.d`, hooki gita |
| system i jego źródła | `/usr`, `/boot`, `/etc/apt`, `/etc/docker`, `fstab`, `sysctl`, `ld.so.preload`, `systemctl mask` |
| kod | pliki skryptów i programów (`.php`, `.py`, `.sh`, `.js`, `.htaccess`...), pliki compose i `Dockerfile` (`compose up -d` jest rutyna, więc to one mówią, co się uruchomi), `pip`/`npm install`, `docker run/exec`, `kubectl apply`, `kubectl set image`, `git pull <adres>`, `bash skrypt.sh`, `apt install ./plik.deb`, `apt -o Dpkg::Pre-Invoke` |
| kontener z dostępem do hosta | `docker run` z `--privileged`, `docker.sock`, `--cap-add`, `--network host`, montowaniem `/` |
| katalog strony | pliki w `/var/www`, `/srv/www`, `public_html`, `public/`, `static/`, `uploads/` -- to, co tam trafi, widać z internetu |
| wystawienie katalogów | konfiguracja serwera WWW z nowym `root`, `alias`, `autoindex`; plik konfiguracji skopiowany spoza katalogu serwera albo treść z innego pliku (`cat x > /etc/nginx/...`) |
| dane wychodzące | `curl`/`wget` do obcych hostów, `git push`, `scp`, `rsync`, `nc` |
| odcięcie dostępu albo Pipe | zatrzymanie `ssh`, `docker`, sieci; kontener i projekt compose samego Pipe; `nginx -s stop`; `kill` |
| restart maszyny | `reboot`, `shutdown` |
| sekrety i dane Pipe | `.env`, klucze, `tokens.json`, katalog danych Pipe |
| zdalne serwery | zmiana plików na celu ssh/docker/kubectl -- tam nie ma kopii po stronie Pipe |
| niewiadome | komenda, której Pipe nie rozpoznaje, podstawienie `$(...)`, zmienne przed komendą (`LD_PRELOAD=`) |

Pytanie wraca też dla każdej zmiany w turze, w której model czytał stronę z internetu, wynik serwera MCP albo
załącznik (jak przy YOLO), dla operacji, o które model sam poprosił (`requires_confirmation: true`), dla zapisu
pamięci Pipe (skille, SERVER.md), nowych celów, rutyn i serwerów MCP oraz zawsze dla roli viewer i w trybie
obserwacji. Błąd reguły oznacza pytanie, nigdy wykonanie.

Dlaczego nieznana komenda zawsze pyta: Pipe czyta logi, strony i pliki, w których obcy może zostawić tekst
udający polecenie. Bez pytania wykona tylko to, co rozpoznaje i co bezpiecznik umie sprawdzić albo cofnąć -- więc
nawet zmanipulowany model nie założy sobie dostępu, autostartu ani nie wyśle danych na zewnątrz. Najgorsze, co może
zrobić bez Ciebie, to rutynowa zmiana z kopią w dzienniku.

Każda taka zmiana jest widoczna: w rozmowie pojawia się „Bez pytania (rutyna, przez bezpiecznik): ...”, pod
komenda jej wynik z dopiskiem *bez pytania*, w dzienniku i audit logu interfejs ma dopisek `[auto]`
(np. `telegram:123 [auto]`). Komendy `host: true` w trybie docker dostają pełny plan bezpiecznika (kopie z katalogu
hosta, sprawdzenia i komendy odwrotne przez kontener pomocniczy). `AUTONOMY=0` w `backend/.env` przywraca pytanie
o każdą zmianę.

## Tryb YOLO (`/yolo`)

Domyślnie rutyna wykonuje się sama, a sprawy ważne czekają na TAK. `/yolo on` (CLI, `pipe web`, Telegram) wyłącza to pytanie -- **tylko w tej
rozmowie (sesji)**, tylko dla administratora i tylko do restartu backendu albo `/yolo off`. Co się nie zmienia:

- klasyfikacja: operacje `forbidden` są nadal odrzucane, a `safe` działają jak zawsze;
- bezpiecznik: każda zmiana idzie przez ten sam plan co po TAK (kopia plików, sprawdzenie przed, weryfikacja po,
  automatyczne przywrócenie configów) i trafia do dziennika -- `/cofnij` działa;
- workery nadal tylko czytają (propozycje zmian nie są wykonywane), a zgody zewnętrznych agentów MCP nadal czekają na decyzję;
- rola viewer nie może włączyć YOLO, a nawet ustawiona flaga nic nie zmienia dla sesji viewera.

Wykonanie bez pytania jest widoczne: klient dostaje `progress` „YOLO — wykonuje bez pytania: ...”, `pipe web` pokazuje
czerwony znacznik nad polem wpisywania, CLI -- `YOLO` w prompcie, a audit log i dziennik zapisują interfejs z dopiskiem
`[yolo]` (np. `cli:kuba [yolo]`). Model dostaje w prompcie blok o trybie YOLO (ostrożność, najmniejsza zmiana,
bez nieodwracalnych operacji bez wyraźnej prośby). YOLO usuwa człowieka z pętli -- włączaj je na czas konkretnej pracy.

## Tryb obserwacji (`PIPE_OBSERVE=1`)

Na początek, zanim zaufasz agentowi, możesz zainstalować go tak, żeby niczego nie zmieniał:
`sudo bash scripts/install-server.sh --profile observe`. Pipe dalej pilnuje serwera, wysyła alerty i raporty,
odpowiada na pytania, rysuje schematy i działa jako bramka MCP do odczytu. Zmiany są odrzucane na dwóch poziomach.

W aplikacji (`runtime.observe()`, także w trybie native):

- komenda zmieniająca stan, zapis pliku, `journal undo`, `cron_manage add/remove` i `pipe_update apply`
  są odrzucane bez pytania o TAK, również w trybie YOLO; model dostaje w prompcie blok o trybie obserwacji,
- `run_command` w bramce MCP odmawia zamiast tworzyć zgodę, a już istniejącej zgody nie da się zatwierdzić,
- `undo` z `execute` (przycisk w `pipe web`, `/cofnij` w CLI i Telegramie) zwraca błąd,
- rejestry i pamięć Pipe działają dalej z potwierdzeniem jak zwykle: rutyny (czytają), cele, SERVER.md, skille.

W kontenerze (`backend/docker-compose.observe.yml`, tylko tryb docker):

- agent nie ma `docker.sock`; Dockera widzi przez `tecnativa/docker-socket-proxy` z API tylko do odczytu
  (`ps`, `inspect`, `logs`, `info` działają; `run`, `exec`, `restart`, `stop` dostają 403). Proxy jest w osobnej
  sieci wewnętrznej, bez dostępu z zewnątrz,
- `/root` hosta jest tylko do odczytu (bez nakładki rw),
- kontener nie ma uprawnień jądra poza `DAC_READ_SEARCH` (czytanie plików) i `NET_RAW` (ping) i działa
  z `no-new-privileges`; `/proc/*/environ` procesów hosta jest wtedy nieczytelny.

Zapisywalne zostają `backend/data` (pamięć Pipe) i `/tmp` hosta (socket dla bota Telegram). W trybie native
obowiązuje tylko ochrona w aplikacji: proces działa jako root. Pełny tryb przywraca
`sudo bash scripts/install-server.sh --profile full`.

## Internet (`web_search`, `web_fetch`, `software_info`)

Agent sam decyduje, kiedy sięgnąć do sieci, więc internet jest traktowany jak niezaufane źródło:

- **Nic poufnego nie wychodzi**: zapytanie, adres i argumenty `software_info` z sekretem (`memory.find_secret()`)
  są odrzucane, prompt każe pisać zapytania ogólnie (treść błędu, oprogramowanie i wersja -- bez domen, IP, nazw
  użytkowników). Każde zapytanie trafia do audit logu.
- **Adres to też kanał wycieku** (`https://obcy.example/?d=<dane>`): `web_fetch` czyta bez pytania tylko adresy
  z wyników `web_search` tej sesji (`Session.web_urls`) albo wpisane przez użytkownika (wiadomości zbudowane przez
  backend, np. alerty z webhooków, się nie liczą). Inny adres -- potwierdzenie.
- **Tylko publiczny internet** (SSRF): host musi wskazywać wyłącznie na adresy publiczne (`ipaddress.is_global`);
  loopback, sieci prywatne, link-local (metadane chmury `169.254.169.254`), CGNAT i adresy IPv4 w IPv6 są
  odrzucane -- przy każdym przekierowaniu osobno. Ograniczenie: sprawdzenie DNS i połączenie to dwa kroki
  (teoretycznie możliwy DNS rebinding); lokalne usługi agent sprawdza przez `network_info`.
- **Prompt injection**: wyniki są oznaczone jako dane, nie polecenia. Stronę z pytaniem czyta osobne zapytanie
  do modelu **bez narzędzi** (`WEB_READER_PROMPT`) -- do agenta trafia tylko odpowiedź. Po przeczytaniu czegokolwiek
  z internetu (`Session.web_tainted`) tryb YOLO jest wstrzymany do następnej wiadomości użytkownika: zmiana
  zaproponowana pod wpływem strony zawsze czeka na TAK.
- Wyszukiwarki bez klucza to nieoficjalne endpointy (DuckDuckGo) i publiczne API (Stack Exchange, Wikipedia,
  endoflife.date, OSV.dev); `WEB_SEARCH=off` zabiera agentowi internet całkowicie.
- **Skan podatności** (czuwanie, bez agenta) wysyła do OSV.dev nazwy i wersje pakietów systemu i pobiera katalog
  CISA KEV; nic więcej nie opuszcza serwera. Wyłącza go `VULN_INTERVAL=0` (niezależnie od `WEB_SEARCH`). Komendę
  łatki składa serwer z nazw pasujących do wzorca nazwy pakietu; klient przysyla tylko token z podglądu.

## Załączniki (pliki, zrzuty ekranu, głosówki)

Plik wysłany przez użytkownika to też obcy tekst -- log może zawierać linię napisaną przez atakującego:

- treść pliku jest opisana w wiadomości jako DANE, nie polecenia, a po wiadomości z załącznikiem tryb YOLO jest
  wstrzymany do następnej wiadomości (`Session.web_tainted`) -- zmiana zaproponowana pod wpływem pliku czeka na TAK,
- pliki tekstowe przechodzą przez `memory.redact_secrets()` przed wysłaniem do modelu (jak wyniki narzędzi),
- tekst z załącznika nie uczy VIBE i nie liczy się jako adres *wpisany przez użytkownika* dla `web_fetch`
  (`pipe_text` trzyma tylko to, co użytkownik napisał),
- obraz jest w historii tylko w swojej turze; pliki binarne leżą w `DATA_DIR/uploads/` z prawami 0600 przez 7 dni,
  zapis trafia do audit logu; viewer nie może zapisać pliku na serwerze (obrazy i tekst -- tak),
- rodzaj pliku wynika z zawartości (sygnatura, UTF-8), nie z nazwy ani typu MIME od klienta; nazwa jest
  sprowadzana do bezpiecznych znaków (bez katalogów),
- limity: 10 MB na plik, 20 MB na wiadomość; `READ_LIMIT` serwera i `MAX_BODY` mostu `pipe web` (32 MiB)
  wynikają z nich.

## Role i tokeny

- `AGENT_TOKEN` -- admin, `AGENT_VIEWER_TOKEN` -- viewer, tokeny klientów z `python3 -m backend.tokens`
  (plik `tokens.json` 0600, tylko skróty SHA-256; odwołanie: `revoke`). Porównanie w stałym czasie, na bajtach.
- Viewer: backend odrzuca `confirm: true` i `undo` z `execute`, a narzędzia zmieniające stan (także zapis pamięci
  Pipe) zwracają modelowi odmowę -- pytanie o TAK nie powstaje. Egzekwuje to backend, nie klient.
- Sesja jest przypięta do tożsamości tokenu, która ją założyła: inny token nie odczyta jej historii ani nie
  zatwierdzi cudzej operacji, nawet znając `session_id`.

## Pipe w internecie (`/web`)

`/web wlacz` wystawia `pipe web` pod publicznym adresem HTTPS (`core/webpublic.py`, `core/webaccess.py`):

- mikr.us (`/web wlacz mikrus` albo sam, gdy na hoście jest `/klucz_api`): Caddy po zwykłym HTTP tylko na IPv6
  (`[::]:7443`), HTTPS kończy proxy mikr.us. Odcinek proxy -> serwer idzie bez TLS przez sieć mikr.us; ciasteczko
  urządzenia ma `Secure`, więc przeglądarka wysyła je tylko po HTTPS. Adres klienta z `X-Forwarded-For` proxy --
  da się go podrobić, łącząc się z portem wprost, ale służy tylko do pokazania i limitu prób (globalny limit
  oczekujących próśb zostaje).
- Tunel (`/web wlacz tunel` albo sam, gdy porty 80/443 są nieosiągalne): kontener `cloudflared` (przypięta wersja)
  bez portów, plików i uprawnień (`--cap-drop ALL`, `no-new-privileges`), we wspólnej sieci z kontenerem Pipe --
  most słucha dla niego tylko na `127.0.0.1:7402`, niewidocznym z zewnątrz. Cloudflare kończy TLS, więc widzi ruch strony (treść rozmowy, ciasteczko
  urządzenia) -- to świadomy kompromis za działanie bez otwartych portów; kto tego nie chce, używa
  `/web wlacz bezposrednio` z własną domeną albo tunelu SSH. Adres klienta z nagłówka `Cf-Connecting-Ip` (ustawia go
  Cloudflare). Nowy adres tunelu po restarcie = nowa domena, więc ciasteczka starych urządzeń nie pasują i trzeba je
  wpuścić jeszcze raz.

- Caddy (`caddy:2.11-alpine`, kontener `pipe-web`) ma sieć hosta, ale tylko gniazdo mostu (`/tmp/pipe-web/web.sock`)
  i wolumen certyfikatów -- bez `docker.sock`, `--cap-drop ALL` (+ `NET_BIND_SERVICE`), `no-new-privileges`,
  limity pamięci i procesów, bez API administracyjnego. Przekazuje adres klienta nagłówkiem, którego klient nie
  podrobi (Caddy go nadpisuje).
- Most w procesie Pipe nie ma tokenu backendu. Bez ciasteczka urządzenia odpowiada tylko strona prośby o dostęp,
  jej pliki i dwa wywołania parowania; reszta to 401. Host musi być adresem strony, Origin -- `https://` tego adresu.
- Parowanie: prośba dostaje kod (6 znaków, bez mylących się liter) i sekret znany tylko stronie, która ją wysłała.
  Zgodę daje administrator z rozmowy (przycisk z backendu albo `/web wpusc <kod>`); kod na ekranie i w wiadomości
  pozwala sprawdzić, że zatwierdzasz swoje urządzenie. Prośba wygasa po 5 minutach, najwyżej 5 próśb na 10 minut
  z jednego IP i 10 oczekujących naraz. Token urządzenia trafia raz do strony, która poprosiła, i tylko jako
  ciasteczko `HttpOnly; Secure; SameSite=Lax` (Lax, żeby link z Telegrama otwierał się zalogowany; API to POST
  z kontrolą Origin). Zatwierdzony, a nieodebrany w 2 minuty token jest usuwany.
- Urządzenie to zwykły token klienta w `tokens.json` (nazwa `web-...`, tylko skrót SHA-256, rola admin albo viewer),
  więc uprawnienia egzekwuje backend jak dla każdego innego tokenu. Wygasa po 30 dniach bez użycia; `/web odwolaj`
  działa od razu, także dla otwartej karty (strumień zdarzeń sprawdza urządzenie co 25 s). Tokeny urządzeń nie
  zamykają trybu bez tokenu -- CLI i Telegram bez `AGENT_TOKEN` działają dalej, a strona i tak wymaga urządzenia.
- Włączenie, wyłączenie, wpuszczanie i odwoływanie -- tylko administrator; każda decyzja trafia do audit logu
  z kodem, urządzeniem i IP. Tryb obserwacji odmawia `/web wlacz`.

Strona w internecie to nowa droga do Pipe, który ma `docker.sock`. Wpuszczaj tylko urządzenia, których kod widzisz
na własnym ekranie, a nieużywane odwołuj. Do samego czytania wystarczy *Tylko podgląd*.

## MCP

**Pipe jako serwer MCP.** Zewnętrzny agent dostaje narzędzia Pipe zamiast powłoki: każda komenda przechodzi przez
klasyfikator, odczyt wykonuje się od razu, zmiana czeka na zgodę administratora (Telegram/CLI) i po niej idzie przez
bezpiecznik i dziennik. Zakazane komendy są odrzucane, pliki z sekretami nie są wydawane, wyniki są redagowane.
Token viewer nie tworzy zgód. Zgodę może sprawdzić tylko agent, który ją utworzył; zgody żyją w pamięci i wygasają
po 30 minutach. `ask_pipe` uruchamia agenta Pipe w roli viewer. Endpoint HTTP: tylko localhost, token Pipe
(`Authorization: Bearer`), walidacja `Origin` (403) i nagłówków 2026-07-28 (`-32020`).

**Pipe jako klient MCP.** W v0.35 klient został usunięty: cudzy serwer działał jako proces Pipe, z jego dostępem do
`docker.sock` i plików. Od v0.42 wraca z inną granicą: serwer stdio (`npx`, `uvx`, obraz Dockera) działa w jednorazowym
kontenerze bez montowań, `docker.sock` i uprawnień (`--cap-drop ALL`, `no-new-privileges`, użytkownik `nobody`, limity),
więc może najwyżej korzystać z sieci i zmieniać swoją usługę (np. repozytorium na GitHubie) -- nie serwer. Inne
programy niż `npx`/`uvx`/obraz i opcje `docker run` typu `-v`, `--network host`, `--privileged` są odrzucane. Sekrety
tylko jako `${MCP_*}` z `backend/.env` albo z `mcp_secrets.json` (0600; wpisywane w oknie `/mcp` w `pipe web` i CLI
komenda `mcp_servers` `klucz`, tylko administrator, wartość nigdy nie wraca do klienta i nie trafia do rozmowy ani
audit logu -- tylko nazwa) -- model nie wskaże `${LLM_API_KEY}` ani tokenu Pipe; tekst wyglądający na sekret
w konfiguracji -- odmowa. Dodatki z katalogu (`backend/addons/`) przechodzą tę samą walidację; `state: true` daje
serwerowi nazwany wolumen `pipe-mcp-state-<nazwa>` na `/state` (nigdy katalog hosta; usuwany razem z serwerem),
a `autoApprove` w katalogu obejmuje tylko logowanie i stan logowania. Dodanie serwera: jedno TAK z podglądem (albo `/mcp dodaj` administratora). Narzędzie bez
`readOnlyHint` -- TAK przy każdym wywołaniu; adnotacje są deklaracją serwera, więc odczyty bez pytania są bezpieczne
tylko o tyle, o ile ufasz temu serwerowi (`askAlways: true` w konfiguracji wyłącza to dla niego). Wynik traktowany jak
treść z internetu (YOLO wyłączone do następnej wiadomości), argument z sekretem nie wychodzi. Tryb obserwacji i viewer:
tylko odczyty. Każde wywołanie jest w audit logu.

## Webhooki

- Serwer webhooków jest domyślnie wyłączony; bez `WEBHOOK_TOKEN` nie startuje. Nasłuchuje na `127.0.0.1`
  (w Dockerze port publikowany tylko na localhost) -- nadawcy spoza serwera przez odwrotne proxy z TLS.
- Treść alertu pochodzi z zewnątrz: trafia do modelu jako dane, a automatyczne badanie robi worker, który
  wykonuje wyłącznie odczyty. Limit badań: raz na 6 h na alert, 10 dziennie. *Napraw* wysyła ustalenia
  agentowi jako zwykła wiadomość -- zmiany i tak przechodzą przez klasyfikator i potwierdzenie.

## Dziennik zmian i cofanie

- Kopie plików sprzed zmian leżą w `backend/data/journal/` (katalogi `0700`, pliki `0600`). Edytowany `.env`
  trafia tam w całości -- to kopia na dysku serwera, nie wysyłana do LLM. Limit: 50 wpisów, 14 dni, 5 MB na plik.
- `/cofnij` pokazuje różnice i komendy odwrotne przed TAK. Komendy odwrotne przechodzą przez klasyfikator:
  zakazana komenda jest pomijana i logowana. Identyfikator wpisu jest walidowany (tylko hex), więc nie wskaże
  ścieżki poza katalogiem dziennika. Przywracanie i komendy odwrotne trafiają do audit logu.
- `/cofnij` z klienta działa bez LLM -- tak jak inne komendy wymaga tokenu (`AGENT_TOKEN`), a w Telegramie
  przycisk *Cofnij* zadziała tylko dla dozwolonego użytkownika, który otworzył podgląd.

## Workery, cele i rutyny

- **Workery i rutyny wykonują tylko `safe`.** Komenda `confirm` nie jest wykonywana; trafia do raportu jako
  propozycja, którą główny agent może wykonać przez `remote_exec` -- z Twoim potwierdzeniem. `forbidden`
  jest logowane jak zawsze.
- **Dodanie celu i rutyny wymaga potwierdzenia** -- zmanipulowany model nie doda po cichu serwera atakującego.
  Potwierdzenie rutyny pokazuje całą treść zadania.
- Pola celu są walidowane wzorcami, komenda idzie jako jeden argument (`shlex.quote`). Na celu-klastrze
  dozwolona jest jedna komenda kubectl/helm (dalsze segmenty `| grep` działają lokalnie) -- druga komenda
  kubectl poszłaby do domyślnego kontekstu.
- SSH w trybie `BatchMode` (bez haseł), `StrictHostKeyChecking=accept-new`, `known_hosts` w `backend/data`.

## Czuwanie

Sprawdzenia są deterministyczne i tylko czytają. Przycisk *Zbadaj* wysyła agentowi wiadomość zbudowana przez
backend (nie przez klienta) -- dalej obowiązują zwykłe zasady. Subskrypcja zdarzeń wymaga tokenu, a bot
wysyła alerty tylko użytkownikom z `TELEGRAM_ALLOWED_USER_IDS`.

---

## Workspace (narzędzia plikowe)

`read_file`, `write_file`, `change_directory` rozwiązują ścieżkę (symlinki, `..`) i odrzucają poniższe ścieżki.
W Dockerze symlink hosta (`/etc/nginx/sites-enabled/x -> /etc/nginx/sites-available/x`) jest rozwiązywany względem
korzenia hosta (`/hostfs`), a `..` nie wychodzi ponad ten korzeń. Odrzucane:
wszystko poza hostem (`/hostfs` w Dockerze), `/boot`, `/dev`, `/proc`, `/sys`, `/var/spool`, katalogi binarek,
`/root/.ssh`, `/home/*/.ssh`, `/etc/shadow`, `/etc/gshadow`, prywatne klucze hosta SSH. Zapis do
`/etc/passwd`, `/etc/shadow`, `/boot`, `/dev` jest zakazany; każdy inny zapis wymaga potwierdzenia.

## Warsztat (`project`) i Pipe piszący pierwszy

Warsztat (`WORKSPACE_DIR`, domyślnie `/root/pipe-workspace`) to jedyne miejsce, w którym Pipe zmienia coś bez
pytania. Granice:

- **Zapis plików bez potwierdzenia** tylko wtedy, gdy podana ścieżka i ścieżka po rozwiązaniu symlinków leżą w
  warsztacie i nie jest to plik startowy (`.bashrc`, cron, systemd...) -- link w sklonowanym repozytorium nie
  wyprowadzi zapisu poza warsztat. Każdy taki zapis jest w audit logu z dopiskiem `[warsztat]`.
- **Kod z repozytorium nigdy nie działa w procesie ani kontenerze Pipe** (tam jest `docker.sock`). `exec` uruchamia
  go w jednorazowym kontenerze z oficjalnego obrazu języka: tylko katalog projektu jako `/work`, bez `docker.sock`,
  `--cap-drop ALL`, `no-new-privileges`, limit pamięci, procesów i czasu. Sieć ma (instalacja zależności).
- **`clone`**: tylko `https://` albo `git@`, `--depth 1`, bez hooków, submodułów i LFS, bez pytania o hasło; za duże
  repozytorium (`PROJECT_MAX_MB`) jest od razu usuwane.
- **README i AGENTS.md to dane.** Po ich przeczytaniu (albo po treści z internetu) `exec` znów wymaga potwierdzenia
  do następnej wiadomości użytkownika -- jak YOLO, tekst z cudzego repozytorium nie uruchomi sam kodu.
- **`run` i `remove` zawsze z potwierdzeniem** z planem. Port tylko na `127.0.0.1`. Compose z `privileged`,
  `docker.sock`, siecią hosta, montowaniem katalogów systemowych albo portem na świat -- potwierdzenie mówi to wprost.
  `run` jest w dzienniku, `/cofnij` usuwa wdrożenie.
- `WORKSPACE_AUTONOMY=0` -- zgoda na każdą operację. Tryb obserwacji i viewer -- tylko lista i logi.

Wiadomości, które Pipe wysyła sam (skrzynka, `core/feed.py`), mają przyciski zbudowane przez backend; klient odsyła
tylko numer wiadomości i akcji (`feed_act`), więc nie da się nim podać własnej komendy. Akcje zmieniające serwer
(*Zwolnij miejsce*, *Załataj*, *Napraw*) są tylko dla administratora. *Napraw* wysyła agentowi propozycję z
samodzielnego badania jako zwykła wiadomość -- zmiany przechodzą przez klasyfikator i potwierdzenie jak zawsze.
Samodzielne badanie to worker tylko z odczytami, z limitem `AUTO_INVESTIGATE_DAILY` dziennie.

## Pamięć agenta

Pamięć trafia do promptu KAZDEJ przyszłej rozmowy (SERVER.md, DIRECTORY, VIBE) albo jest ładowana na żądanie
(skille), więc jeden udany prompt injection mógłby zaszczepić tam trwałą instrukcję. Obrona:

- **Zapis/zastapienie skilla i pełne nadpisanie SERVER.md wymagają potwierdzenia** (z podglądem treści/diffem) --
  najsilniejsze wektory trwałej injekcji nie przechodzą bez Twojego TAK.
- **Każdy inny zapis pamięci jest zgłaszany widocznym komunikatem `[PAMIEC]`** (Telegram i CLI) -- pamięci
  nie da się zmienić po cichu, nawet gdy model próbuje to ukryć w swojej odpowiedzi.
- każdy zapis jest w audit logu (ścieżka, bez treści),
- oczywiste sekrety są odrzucane, a z adresow remote repozytoriów usuwane są dane logowania,
- w prompcie treść jest oznaczona jako **dane, nie polecenia** -- nie zmienia klasyfikacji ani potwierdzeń;
  VIBE to tylko wskazówki stylu.

Skille z cudzych repozytoriów (`/pobierz`, `skill_manage install`) to ten sam wektor: przed instalacją widać podgląd
(nazwy, opisy, źródło, co pominięte) i trzeba kliknąć *Zainstaluj* albo dać TAK; do pamięci trafia sam tekst
`SKILL.md` z dopisanym źródłem, skrypty i pliki pluginu nie (haki, komendy, agenty i programy -- nigdy). Repozytorium
jest klonowane jak w warsztacie (bez hooków, submodułów, LFS, limit rozmiaru) i usuwane po odczycie; treść to dane --
YOLO jest wyłączone do następnej wiadomości. Instaluj tylko z zaufanych źródeł.

Mimo to przeglądaj od czasu do czasu `backend/data/` (`/server`, `/katalogi`, `/vibe`, `/skille`) -- ramowanie
"dane, nie polecenia" nie jest gwarancja.

---

## Ograniczenia, o których trzeba wiedzieć

- **`docker.sock` = root na hoście.** Kto może wykonać `docker run -v /:/x`, ma pełny dostęp do hosta --
  montowanie `/` jako read-only niewiele tu zmienia. Dlatego `docker run/exec` zawsze wymaga potwierdzenia,
  ale ostatecznym zabezpieczeniem jest Twoje TAK.
- **Komendy na hoście (tryb docker).** System plików hosta jest w kontenerze tylko do odczytu, więc zmiany na
  serwerze (`apt-get`, `systemctl`, `sed -i` w `/etc`, `ufw`, łatki z `/zalataj`) Pipe wykonuje przez krótki
  kontener pomocniczy z własnego obrazu: `docker run --rm --privileged --pid=host`, a w nim `nsenter -t 1` do
  przestrzeni nazw hosta -- komenda działa jak w powłoce roota na serwerze. To nie jest nowe uprawnienie (patrz
  punkt wyżej), tylko jedna przewidywalna droga: model prosi o nią przez `execute_command` z `host: true`,
  klasyfikator ocenia komendę docelową, zakazane są odrzucane, rutynę (sekcja *Samodzielność*) Pipe wykonuje sam,
  a reszta czeka na Twoje TAK.
  W potwierdzeniu widać komendę docelową i linie *na hoście: kontener pomocniczy...*; opakowanie składa kod,
  komenda trafia do niego jako jeden argument. Tryb obserwacji i rola viewer tej drogi nie mają. Bezpiecznik działa
  jak dla komendy lokalnej: kopie plików czyta z katalogu hosta, sprawdzenia (`nginx -t`, `systemctl is-active`)
  i komendy odwrotne idą tym samym kontenerem pomocniczym, a nieudany config wraca z kopii. Jeśli nie potrzebujesz zarządzania kontenerami,
  zainstaluj Pipe z `--profile observe` (patrz wyżej) albo usuń montowanie `docker.sock` z `docker-compose.yml`.
- **`/zalataj` nie pyta o każdą komendę.** Wpisanie `/zalataj` (albo przycisk *Załataj*) przez administratora jest
  zgoda na całą łatkę: Pipe od razu instaluje poprawki pakietów tego serwera, podbija zależności aplikacji (menedżer
  pakietów w jednorazowym kontenerze jak `exec` w warsztacie: tylko katalog aplikacji, bez `docker.sock` i uprawnień,
  bez skryptów instalacyjnych pakietów; nazwy i wersje z lockfile'i przechodzą przez wzorce ekosystemu; kopia plików w
  dzienniku), odświeża obrazy kontenerów z compose (w Dockerfile zmienia tylko tag w linii `FROM`, w tej samej linii
  wersji, z kopią pliku w dzienniku),
  łata zdalne serwery (cele ssh: jako root albo przez `sudo -n`) i sprząta po sobie (obrazy bez tagu, cache
  budowania starszy niż doba, pobrane paczki, cache npm/pip/yarn/composer, stare jądra, które apt sam uznaje za
  zbędne). Komendy składa serwer z wyniku skanu, bez modelu: nazwy pakietów i projektów compose przechodzą przez
  wzorce, więc treść z bazy pakietów ani z etykiet kontenera nie doklei nic do powłoki. Agent w rozmowie (narzędzie
  `security_patch`) dostaje tę samą łatkę bez pytania (rutyna; przy `AUTONOMY=0` za jednym Twoim TAK). Viewer i tryb obserwacji jej
  nie mają. Obrazu, który nie ma nowszej wersji, Pipe nie zostawia: instaluje w nim poprawki z repozytorium jego
  systemu (kontener tymczasowy z tego obrazu, `docker commit` pod tym samym tagiem). Poprzedni obraz zostaje pod tagiem
  `pipe-rollback-*`, a `/cofnij` do niego wraca.
- Rozmowy leżą na dysku w `DATA_DIR/sessions` (prawa 0600, katalog 0700) przez `SESSION_KEEP_DAYS` dni, żeby
  przetrwały restart. Wyniki narzędzi są w nich zredagowane jak w zapytaniach do modelu, ale to, co sam wpiszesz,
  zostaje. `SESSION_KEEP_DAYS=0` wyłącza zapis i przy starcie usuwa zapisane sesje.
- Klasyfikator nie jest sandboxem. Rozpoznaje wzorce; potwierdzenie jest po to, żebyś widział, co się wykona.
  Czytaj komendy przed TAK.
- Tryb native i nakładka Kubernetes `host-agent` działają jako root na maszynie.
- Nakładka Kubernetes `operator` pozwala usuwać pody i skalować workloady (z potwierdzeniem).

## Audit log

```
[2026-09-24 14:23:11] [cli:kuba] [SAFE] df -h /hostfs → exit_code=0
[2026-09-24 14:25:03] [telegram:123456789] [CONFIRMED] docker restart shop-web → exit_code=0
[2026-09-24 14:26:44] [telegram:123456789] [BLOCKED] execute_command(rm -rf /) → FORBIDDEN
[2026-09-24 14:30:02] [worker:web-1@telegram:123456789] [SAFE] ssh ... web-1 -- 'uptime' → exit_code=0
```

Zawartość plików (read_file, write_file, pamięć) nigdy nie trafia do logu. Znaki nowej linii w komendzie są
escapowane (`\n`), więc komenda z `\n` nie sfalszuje kolejnych wpisów. Token porównywany jest w stałym czasie
(`hmac.compare_digest`).

## Zalecenia

1. **`AGENT_TOKEN`** -- kreator i `scripts/install-server.sh` generują go automatycznie; w trybie kubernetes
   jest wymagany (backend nie wystartuje bez niego). Ustawiasz ręcznie tylko przy zmianie.
2. Nie publikuj portu 7379 na publicznym interfejsie -- dostęp przez tunel SSH albo `kubectl port-forward`.
   Nakładka Kubernetes dokłada `NetworkPolicy` (deny z innych podów) obok tokenu.
3. Ogranicz `TELEGRAM_ALLOWED_USER_IDS` do minimum.
4. Dla workerów na innych serwerach używaj dedykowanego klucza SSH i użytkownika z ograniczonymi uprawnieniami.
5. Czytaj komendy przed potwierdzeniem i przeglądaj audit log (`/historia`) oraz pamięć (`/skille`, `/server`).

## Klucze providerów LLM dodane z interfejsu

`pipe web` pozwala dodać klucz API kolejnego providera. Klucz idzie z przeglądarki do lokalnego mostu (`127.0.0.1`),
tunelem SSH do backendu i tam zostaje w `DATA_DIR/llm_keys.json` (0600). Nie wraca do klienta (lista providerów podaje
tylko, czy klucz jest i skąd), nie trafia do audit logu (logowana jest sama zmiana providera) ani do modelu. Zanim
zostanie zapisany, backend sprawdza go u providera -- wyłącznie pod adresem z presetu albo z pliku providerów, więc
klucza nie da się wysłać pod dowolny adres. Zmiany może robić tylko administrator. Odczyt `llm_keys.json` przez
agenta jest klasyfikowany jak `.env` (wymaga potwierdzenia), a redakcja sekretów obejmuje wyniki narzędzi jak zwykle.

## Aktualizacja samego Pipe (`pipe_update`)

To narzędzie pobiera kod z sieci i uruchamia go z uprawnieniami agenta, więc jest celowo wąskie:

- nie przyjmuje adresu, gałęzi, komendy ani ścieżki -- model może tylko wybrać `check` albo `apply`;
- źródłem jest wyłącznie `origin` repozytorium, w którym Pipe jest zainstalowany, przez `git pull --ff-only`
  (bez nadpisywania historii i bez scalania);
- `apply` wykonuje się bez pytania jak inna rutyna (przy `AUTONOMY=0` po potwierdzeniu), a rola viewer jest
  odrzucana z góry;
- projekt, katalog i usługi compose pochodzą z etykiet Dockera własnego kontenera i są cytowane pojedynczo;
- naraz działa jedna aktualizacja; kontener pomocniczy dostaje `docker.sock` i katalog repozytorium, nic więcej.

Zaufanie przenosi się na zdalne repozytorium: kto może wypchnąć commit na śledzona gałąź, ten przy następnej aktualizacji
uruchomi kod na serwerze. To ta sama granica co przy ręcznym `git pull`, ale warto o niej pamiętać.
