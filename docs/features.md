# Workery, cele, rutyny, czuwanie, diagramy i historia serwera -- Pipe

Pipe v0.16.1

Ten dokument opisuje funkcje, ktore odrozniaja Pipe od agentow ogolnego przeznaczenia:
agent, ktory sam pilnuje serwera, widzi jego architekture i zarzadza wieloma maszynami naraz.

---

## Diagramy

Agent rysuje diagramy w skladni **Mermaid** i wysyla je jako obraz:

- **Telegram** -- zdjecie (duze diagramy jako plik w pelnej rozdzielczosci)
- **CLI** -- PNG w `~/.pipe/diagrams/` + podglad ASCII w terminalu, jesli sie miesci
  (`pipe --open` otwiera PNG automatycznie; `/mermaid` pokazuje kod ostatniego diagramu)

Dwa tryby narzedzia `diagram`:

| Tryb | Kiedy | Skad dane |
|------|-------|-----------|
| `infra` | *"pokaz architekture"*, *"co tu jest postawione"*, `/mapa` | automatyczne odkrycie (ponizej) |
| `mermaid` | przeplywy, procedury, zaleznosci, *"narysuj jak dziala deploy"* | kod napisany przez agenta |

**Mapa infrastruktury** (`/mapa` dziala bez LLM -- szybko i za darmo) sklada:

- kontenery z `docker inspect`: obraz, stan, healthcheck, opublikowane porty, projekt compose
  (kontenery projektu sa w ramce, aplikacja -> baza polaczone linia przerywana)
- trasy reverse proxy: `server_name` / `proxy_pass` / `upstream` z nginx (`sites-enabled`, `conf.d`),
  bloki `reverse_proxy` z Caddyfile, etykiety Traefika (`Host(...)`) i `VIRTUAL_HOST`
- porty hosta z `/proc/1/net` -- porty wystawione na swiat sa polaczone z *Internet*
- wlaczone uslugi systemd (bez systemowych)
- aplikacje z klastra Kubernetes (ingress -> service -> deployment), gdy kubectl ma dostep
- zdalne cele Pipe

Kontener wylaczony albo `unhealthy` jest czerwony. Etykiety sa escapowane -- nazwa kontenera
czy domena nie zlamie skladni diagramu.

Renderowanie: [mermaidx](https://github.com/MohammadRaziei/mermaidx) -- prawdziwa biblioteka Mermaid
w osadzonym QuickJS + resvg. Bez przegladarki, bez Node.js i bez wysylania kodu diagramu do
zewnetrznej uslugi (w przeciwienstwie do mermaid.ink/kroki). Blad skladni wraca do modelu, ktory
poprawia kod.

---

## DIRECTORY -- mapa katalogow

Uzupelnienie SERVER.md: zamiast prozy -- lista konkretnych miejsc z jednozdaniowym opisem.

```
- /srv/shop [repo] sklep (Next.js), deploy przez compose (remote https://github.com/acme/shop.git, galaz main)
- /srv/shop/docker-compose.yml [compose] projekt docker compose 'shop'
- /var/backups/pg [backup] nocne dumpy Postgresa, rotacja 7 dni
```

- `directory operation=scan` odkrywa repozytoria git (bez uruchamiania gita: `.git/config`,
  `.git/HEAD`; tokeny z adresow remote sa usuwane) pod `/root`, `/home`, `/srv`, `/opt`, `/var/www`,
  `/data` oraz katalogi projektow compose z etykiet kontenerow. Skan nie nadpisuje opisow agenta.
- Agent dopisuje miejsca sam, gdy na nie trafi. Gdy mowisz o projekcie, najpierw sprawdza DIRECTORY.
- `/katalogi` w obu klientach. Plik: `backend/data/directory.json`.

---

## VIBE -- styl rozmowy

Agent z czasem dopasowuje sie do tego, jak piszesz i czego oczekujesz.

- Co `VIBE_EVERY` (domyslnie 6) Twoich wiadomosci agent w tle (bez spowalniania odpowiedzi) wysyla do LLM
  obecna notatke i Twoje ostatnie wiadomosci, i zapisuje zaktualizowana notatke: ton, dlugosc odpowiedzi,
  poziom techniczny, nawyki, czego unikac.
- Gdy powiesz wprost (*"odpowiadaj krocej"*), agent zapisze to od razu (narzedzie `vibe`).
- Osobna notatka na uzytkownika: `vibe/telegram-123.md`, `vibe/cli-kuba.md`.
- Do nauki trafiaja tylko Twoje wiadomosci -- bez promptow skladanych przez Pipe (skan, `/status`, skille).
- VIBE to wskazowki stylu: nie zmienia zasad bezpieczenstwa, potwierdzen ani formatu interfejsu.
- `/vibe` pokazuje notatke, `/vibe reset` ja czysci. `WORKER_MODEL` ustawia tanszy model tez dla VIBE.

---

## Zdalne cele

Cel to miejsce, na ktorym Pipe wykonuje komendy **bez instalowania czegokolwiek po drugiej stronie**:

| Rodzaj | Jak Pipe wykonuje komende | Pola |
|--------|---------------------------|------|
| `ssh` | `ssh -o BatchMode=yes ... host -- 'komenda'` | `host`, `user`, `port`, `identity_file` |
| `docker` | `docker exec kontener sh -c 'komenda'` | `container` |
| `kubernetes` (klaster) | `kubectl --context C --namespace N ...` | `context`, `namespace` |
| `kubernetes` (pod) | `kubectl exec pod -- sh -c 'komenda'` | `pod`, `pod_container` |
| `local` | ten host (zawsze dostepny) | -- |

- Dodanie celu wymaga potwierdzenia. Pola sa walidowane, komenda idzie jako jeden argument
  (`shlex.quote`) -- nazwa ani parametry nie wstrzykna polecen.
- Klasyfikator ocenia komende **docelowa**, a potwierdzenie pokazuje pelna komende z opakowaniem.
- SSH uzywa kluczy z `/root/.ssh` (w Dockerze tylko do odczytu); `known_hosts` celow jest w `backend/data`.
  Uwierzytelnianie haslem nie jest obslugiwane (BatchMode) -- dodaj klucz publiczny Pipe na celu.
- Klaster: w trybie Docker odkomentuj montowanie `/root/.kube` w `docker-compose.yml`; w trybie
  Kubernetes Pipe uzywa wlasnego ServiceAccount.
- `/cele` w obu klientach. Plik: `backend/data/targets.json`.

Przyklad: *"dodaj serwer 10.0.0.5 jako db-backup, ssh, uzytkownik root"* -> TAK -> *"sprawdz lacznosc"*.

---

## Workery

Worker to pod-agent, z ktorym rozmawia agent, a nie Ty. Narzedzie `delegate` wysyla kilka naraz:

```
Ty:     sprawdz, czemu na obu serwerach www rosnie load
Agent:  delegate [web-1: "load, top procesy, logi nginx z ostatniej godziny"],
                 [web-2: to samo]
          › web-1: start (web-1)
          › web-1 $ uptime
          › web-2 $ ps aux --sort=-%cpu | head
          › web-1: gotowe (4 komend)
Agent:  Na web-1 load robi cron z backupem o 14:00, web-2 -- php-fpm; proponuje ...
```

- Kazdy worker: jeden cel, jedno zadanie, wlasna historia rozmowy z LLM, jedno narzedzie `run`.
- **Tylko odczyty.** Komenda zmieniajaca stan nie jest wykonywana -- trafia do raportu jako propozycja.
  Agent wykonuje ja przez `remote_exec`, czyli za Twoja zgoda. Dzieki temu workery moga dzialac
  rownolegle i w tle bez nikogo przy klawiaturze.
- Raport: USTALENIA / PROBLEMY / PROPOZYCJE. Postep widac na zywo (Telegram: jedna aktualizowana wiadomosc).
- Ta sama nazwa workera w kolejnym `delegate` kontynuuje jego watek.
- Limity: `MAX_WORKERS` (6), `WORKER_TIMEOUT` (240 s), `WORKER_MAX_ITERATIONS` (8).
  `WORKER_MODEL` -- tanszy model dla workerow.

---

## Rutyny

Zadania, ktore agent wykonuje sam wedlug harmonogramu i raportuje:

*"codziennie o 7 sprawdz backupy w /var/backups i waznosc certyfikatow, pisz tylko jak cos jest nie tak"*

- Harmonogram cron (5 pol, czas serwera) albo `@hourly` / `@daily` (7:00) / `@weekly` / `@monthly`.
- Wykonuje je worker na wskazanym celu -- tylko odczyty.
- Raport zaczyna sie od `STATUS: OK` albo `STATUS: PROBLEM`. `notify=problems` wysyla tylko problemy.
- Raporty przychodza na Telegram (subskrypcja zdarzen). `/rutyny` pokazuje liste i ostatni status.
- Dodanie rutyny wymaga potwierdzenia. *"uruchom rutyne poranny-przeglad teraz"* -- `routine_manage run`.

---

## Przypomnienia

Rutyna jest cykliczna; przypomnienie odpala sie **raz**, o podanym czasie, i znika:

*"napisz do mnie za 10 sekund"* · *"przypomnij jutro o 9 o odnowieniu domeny"* · *"za godzine sprawdz, czy backup sie skonczyl"*

- Czas wzgledny (`10s`, `15m`, `2h`, `1d`, `1h30m`) albo bezwzgledny (`HH:MM`, `RRRR-MM-DD HH:MM`, czas serwera).
- **Wiadomosc** (`kind=message`) -- sama tresc, bez LLM i bez potwierdzenia; ustawienie widac jako notke `[PAMIEC]`.
- **Zadanie** (`kind=task`) -- o czasie worker wykonuje je na wskazanym celu (tylko odczyty) i przysyla raport.
  Wymaga potwierdzenia, bo wykona sie bez nadzoru.
- Dostarczenie: Telegram, kanalem zdarzen. Przypomnienie z Telegrama wraca do osoby, ktora je ustawila; z CLI --
  do administratorow. Gdy bot nie jest podlaczony, przypomnienie czeka i dociera po polaczeniu.
- CLI nie odbiera zdarzen na zywo -- zalegle przypomnienia pokazuje po polaczeniu i przy najblizszej wiadomosci.
- Rejestr `backend/data/reminders.json` przetrwa restart; limit 50 przypomnien, najdalej rok naprzod.
- `/przypomnienia` -- lista z czasem serwera; `/przypomnienia anuluj <id>` -- anulowanie (bez LLM).

Agent nie potrafi czekac w trakcie odpowiedzi. Bez tego narzedzia obietnica *"odezwe sie pozniej"* bylaby pusta --
prompt zabrania jej, jesli nic nie zostalo zaplanowane.

---

## Czuwanie

Deterministyczne sprawdzenia co `WATCH_INTERVAL` (120 s), bez LLM i bez kosztow:

| Sprawdzenie | Prog | Poziom |
|-------------|------|--------|
| Zajetosc kazdego dysku hosta | `WATCH_DISK_PCT` (90%), >= 97% krytyczny | ostrzezenie / krytyczny |
| RAM | `WATCH_MEM_PCT` (92%) | ostrzezenie |
| Load average 5 min | rdzenie x `WATCH_LOAD_FACTOR` (2) | ostrzezenie |
| Kontener restartuje sie w petli | -- | krytyczny |
| Kontener `unhealthy` | -- | ostrzezenie |
| Kontener, ktory dzialal, przestal dzialac | -- | krytyczny (jednorazowo) |
| **Nowy publiczny port na hoscie** | wzgledem poprzedniego sprawdzenia | ostrzezenie (jednorazowo) |

- Alert jest wysylany raz, a gdy problem minie -- przychodzi *ROZWIAZANE*.
- Telegram: alert z przyciskiem **Zbadaj** -- agent bada przyczyne (tylko odczyty) i proponuje naprawe.
- Aktywne alerty sa w prompcie agenta -- pytany o stan serwera, uwzgledni je. `/alerty` w obu klientach.
- Stan (poprzednie porty i kontenery) w `backend/data/watch_state.json`. `WATCH_ENABLED=0` wylacza.

---

## Co sie zmienilo -- wehikul czasu serwera

Najczestsze pytanie przy awarii: *"co sie zmienilo, ze przestalo dzialac?"*. Pipe zna odpowiedz, bo co
`SNAPSHOT_INTERVAL` (1 h) robi migawke hosta -- same odczyty plikow i `docker inspect`, bez LLM:

| Sekcja | Co widac |
|--------|----------|
| System | jadro, restart serwera (nowy rozruch) |
| Pakiety | instalacje, aktualizacje, usuniecia (dpkg, apk) |
| Kontenery | nowe i usuniete, zmiana obrazu **albo nowej wersji obrazu pod tym samym tagiem**, zatrzymanie |
| Porty | nowe i zamkniete porty TCP hosta |
| Uslugi | wlaczone/wylaczone uslugi systemd, zmienione pliki jednostek |
| Cron | dodane i usuniete linie (sekrety zredagowane) |
| Konta | nowe konta z powloka albo uid 0 |
| Klucze SSH | nowe klucze w `authorized_keys` (odcisk + komentarz; sam klucz nie jest zapisywany) |
| Konfiguracja | sshd, sudoers, nginx, Caddy, HAProxy, fstab, hosts, `daemon.json`, pliki compose projektow |

- Migawka jest zapisywana tylko, gdy cos sie zmienilo. Ostatnie 48 h -- wszystkie, starsze -- jedna na dzien,
  maksymalnie `SNAPSHOT_KEEP_DAYS` (30) dni. Pliki: `backend/data/snapshots/`.
- `/zmiany [24h|3d]` pokazuje zmiany pogrupowane w przedzialy *"miedzy 03:00 a 04:00"*.
- Agent sam zaczyna od historii zmian, gdy slyszy *"przestalo dzialac"*, a przy **Zbadaj** (alert) backend
  dolacza zmiany z ostatniej doby do polecenia.
- Zmiany kont, kluczy SSH, sudoers i sshd sa oznaczone **[BEZPIECZENSTWO]**.

---

## Wykresy

Czuwanie przy kazdym sprawdzeniu (co 2 min) zapisuje probke: load, RAM, swap, zajetosc dyskow. Historia
(`METRICS_KEEP_DAYS`, 8 dni) daje wykresy bez Prometheusa:

- `/wykres` -- load z ostatniej doby; `/wykres ram 7d`, `/wykres dysk 3d`
- *"czy RAM rosnie od tygodnia?"* -- agent rysuje wykres (narzedzie `server_history`, `operation=chart`)
  i dostaje liczby (min, max, srednia, przekroczenia progu), bo sam obrazu nie widzi
- Czerwona linia na wykresie to prog alertu czuwania.

Wykres to Mermaid `xychart-beta` renderowany tym samym silnikiem co diagramy (offline).

---

## Monitoring bez konfiguracji

Pipe wie, co jest na serwerze, wiec sam wie, co sprawdzac -- co `CHECKS_INTERVAL` (1 h):

| Sprawdzenie | Skad Pipe wie, co sprawdzic | Alert |
|-------------|-----------------------------|-------|
| Waznosc certyfikatu TLS | domeny z nginx, Caddy, etykiet Traefika / `VIRTUAL_HOST` | `WATCH_CERT_DAYS` (14) dni przed wygasnieciem; krytyczny 3 dni przed albo gdy certyfikat jest nieprawidlowy |
| Odpowiedz HTTPS | te same domeny | 5xx albo brak odpowiedzi **dwa razy z rzedu** (chwilowe 502 przy deployu nie alarmuje) |
| DNS | te same domeny | domena sie nie rozwiazuje; w `/zdrowie` -- czy wskazuje na ten serwer |
| Swiezosc backupow | wpisy `[backup]` w DIRECTORY | najnowszy plik starszy niz `WATCH_BACKUP_HOURS` (26 h), pusty albo brakujacy katalog |

- `/zdrowie` -- wszystko naraz, z liczba dni do wygasniecia kazdego certyfikatu.
- Katalog backupow dopiszesz zdaniem: *"backupy bazy leza w /var/backups/pg"* (agent doda wpis `[backup]`).
- `WATCH_SITES=0` wylacza sprawdzenia sieciowe, `WATCH_IGNORE=a.example.com,/var/backups/stare` pomija wybrane.

---

## Poranny raport

Codziennie o `DIGEST_TIME` (7:00, czas serwera; `off` wylacza) na Telegram przychodzi raport -- **bez LLM**:

- **Stan** -- uptime, load wzgledem rdzeni, RAM, dyski
- **Alerty** -- aktywne i zdarzenia z ostatniej doby
- **Zmiany od wczoraj** -- najpierw zmiany bezpieczenstwa, potem kontenery, porty, uslugi; pakiety zbiorczo
- **Zdrowie** -- najblizej wygasajace certyfikaty, strony, backupy
- **Aktualizacje** -- liczba pakietow do aktualizacji i *wymagany restart* (Ubuntu/Debian)
- **Rutyny** -- ostatni status kazdej rutyny
- **LLM** -- zuzycie tokenow z poprzedniego dnia

Do raportu dolaczony jest wykres obciazenia z ostatniej doby. `/raport` -- raport na zadanie.

---

## Koszty LLM

Kazde zapytanie do providera (rozmowa, workery, rutyny, VIBE) jest liczone w `backend/data/usage.json`.

- `/koszt` -- tokeny dzis (z podzialem na uzytkownika, workery, rutyny, VIBE i model), ostatnie dni, miesiac.
- Koszt w USD pojawi sie po podaniu cen modelu: `LLM_PRICE_IN` i `LLM_PRICE_OUT` (USD za milion tokenow),
  osobno `WORKER_PRICE_IN/OUT` dla `WORKER_MODEL`.
- `DAILY_TOKEN_LIMIT` / `DAILY_COST_LIMIT` -- po przekroczeniu agent odpowiada komunikatem o limicie do polnocy
  i nie wysyla zapytan do providera. Czuwanie, raport i komendy bez LLM (`/mapa`, `/zmiany`, `/wykres`) dzialaja dalej.

---

## Zmiany z bezpiecznikiem i `/cofnij`

Kazda operacja, ktora zatwierdzasz, dostaje **plan** -- widoczny w potwierdzeniu, zanim nacisniesz TAK:

```
WYMAGA POTWIERDZENIA: `sed -i 's/8080/8081/' /etc/nginx/sites-enabled/shop && systemctl reload nginx`
Bezpiecznik:
- kopia przed zmiana: /etc/nginx/sites-enabled/shop
- sprawdzenie przed (niepowodzenie = nie wykonam): nginx -t
- weryfikacja po: nginx -t; usluga nginx aktywna; strony, ktore dzialaja teraz, maja dzialac po zmianie
- jesli weryfikacja nie przejdzie: przywroce pliki z kopii automatycznie i przeladuje ponownie
- cofniecie pozniej: /cofnij
```

| Krok | Co Pipe robi |
|------|--------------|
| Kopia | pliki zmieniane przez `sed -i`, `tee`, `>`/`>>`, `cp`, `mv`, `rm`, `chmod`/`chown`, `write_file`; HEAD repozytorium przy `git pull/checkout/reset/commit`; crontab; pliki pamieci Pipe przy zmianie celow, rutyn, skilli i SERVER.md |
| Przed | walidacja konfiguracji przed (prze)ladowaniem: `nginx -t`, `sshd -t` (chroni przed odcieciem SSH), `caddy validate`, `apachectl configtest`, `haproxy -c`, `postfix check`, `docker compose config -q`; w kontenerze -- `docker exec <nginx> nginx -t`. **Nieudane sprawdzenie = operacja nie jest wykonywana** |
| Po | usluga `active`, kontener `running` i nie `unhealthy`, wszystkie kontenery projektu compose wstaly, zmieniony plik przechodzi walidacje (nginx, sshd, sudoers `visudo -c`, fstab, compose, JSON), **strony, ktore odpowiadaly przed zmiana, odpowiadaja po niej** |
| Przywrocenie | nieudana weryfikacja zmiany plikow konfiguracji -> kopia wraca automatycznie (`SAFE_AUTO_ROLLBACK=1`), a usluga jest przeladowana ponownie. Inne operacje -- wynik trafia do agenta, a Ty mozesz `/cofnij` |

Plan powstaje z analizy komendy w kodzie (bez LLM); wykonywany jest dokladnie plan, ktory widziales.
Program, ktorego nie ma w miejscu dzialania Pipe (np. `sshd` w kontenerze), jest pomijany i zglaszany jako
*nie sprawdzono* -- wtedy plan mowi to wprost.

**Dziennik i cofanie.** `/dziennik` pokazuje ostatnie zmiany (`backend/data/journal/`, 50 wpisow, 14 dni).
`/cofnij` (albo *"cofnij ostatnia zmiane"*) pokazuje roznice plikow i komendy odwrotne -- `docker stop` ->
`docker start`, `systemctl disable` -> `enable`, `git pull` -> `git reset --keep <poprzedni HEAD>`,
`docker compose down` -> `up -d` -- i czeka na TAK. `/cofnij` dziala bez LLM, wiec pomaga tez wtedy, gdy
provider nie odpowiada albo skonczyl sie dzienny limit. Cofniecie tez trafia do dziennika -- mozna je cofnac.

Czego Pipe nie cofa automatycznie (i mowi to w planie): instalacji pakietow, zmian w klastrze Kubernetes,
usunietych kontenerow i wolumenow, `git clean`, katalogow usuwanych `rm -r`, poprzednich wersji obrazow.

---

## Audyt bezpieczenstwa i straznik

`/audyt` (albo *"jak bezpieczny jest ten serwer?"*) -- ocena 0-100 bez LLM, kazdy punkt z poprawka:

| Obszar | Co sprawdza |
|--------|-------------|
| SSH | logowanie haslem (z uwzglednieniem `sshd_config.d` i zasady "pierwsza wartosc wygrywa"), root z haslem, klucze |
| Zapora | ufw, firewalld, nftables, iptables (zapora w panelu dostawcy -- Pipe tego nie widzi i mowi to) |
| Porty | bazy danych, Redis, Elasticsearch, API Dockera/Kubernetesa na publicznym adresie; port publikowany przez kontener dostaje poprawke w compose, bo **Docker omija ufw** |
| Kontenery | `privileged`, zamontowany `docker.sock`, siec hosta |
| Konta | uid 0 poza rootem, konta bez hasla (z `/etc/shadow` odczytywane sa tylko nazwy kont) |
| Aktualizacje | unattended-upgrades, oczekujace poprawki bezpieczenstwa, wymagany restart |
| Ochrona | fail2ban / CrowdSec (wazniejsze, gdy SSH przyjmuje hasla) |
| Sekrety | pliki `.env` czytelne dla wszystkich w katalogach aplikacji z DIRECTORY |
| Higiena | synchronizacja czasu, swap na malej maszynie, certyfikaty |

*"napraw 1"* -- agent wykonuje poprawke zwyklym narzedziem, wiec dostajesz ja do zatwierdzenia z planem
bezpiecznika (kopia, `sshd -t` przed przeladowaniem, `/cofnij`). Poprawka SSH trafia do wlasnego pliku
`sshd_config.d/00-pipe-*.conf`, zeby wygrac z ustawieniami cloud-init. Bez klucza w `authorized_keys` Pipe
**nie proponuje** wylaczenia hasel -- najpierw klucz (skill `utwardz-ssh`). W trybie docker komendy sa oznaczone
*w powloce hosta* (system plikow hosta jest tylko do odczytu).

**Straznik** (co `WATCH_INTERVAL`, bez LLM) porownuje konta, klucze SSH, programy SUID/SGID i konfiguracje
uwierzytelniania (sudoers, sshd, PAM, `/etc/passwd`, `ld.so.preload`). Nowe konto z uid 0, nowy klucz, nowy
SUID albo `ld.so.preload` -- alert krytyczny. Zmiany zrobione przez Pipe (sa w dzienniku) nie alarmuja.

**Log SSH** (`/var/log/auth.log` albo `/var/log/secure`, czytany przyrostowo): seria nieudanych logowan,
udane logowanie haslem z adresu, ktory wczesniej zgadywal hasla, logowanie z nowego adresu.

---

## Pierwsze 5 minut

Po instalacji, gdy bot Telegram polaczy sie pierwszy raz, Pipe sam pisze: mapa serwera jako obraz, ocena
bezpieczenstwa z trzema najwazniejszymi poprawkami i lista tego, czego od teraz pilnuje. CLI pokazuje to samo
przy pierwszym polaczeniu z danym serwerem. Bez LLM.

---

## Wbudowane skille

| Skill | Co robi |
|-------|---------|
| `nginx-vhost` | nowa domena w nginx jako reverse proxy + certyfikat Let's Encrypt |
| `swap` | plik swap z wpisem w fstab (tez btrfs) |
| `fail2ban-ssh` | fail2ban z jailem SSH, bez blokowania wlasnego adresu |
| `backup-postgres` | nocny `pg_dump` z rotacja, wpis `[backup]` w DIRECTORY (czuwanie pilnuje swiezosci) |
| `aktualizuj-kontener` | pull + up uslugi compose z planem powrotu do poprzedniego obrazu |
| `utwardz-ssh` | wylaczenie hasel w SSH w kolejnosci, ktora nie odetnie dostepu |
| `wolne-miejsce` | co zajmuje dysk i bezpieczne sprzatanie, krok po kroku |

Instalowane przy starcie do `backend/data/skills/` jak zwykle skille -- masz je w `/skille` i jako komendy
(`/nginx_vhost`). Mozesz je edytowac i usuwac: nowa wersja z aktualizacji Pipe zastapi tylko skill, ktorego
nie zmieniales, a usunietego nie przywroci.

Przy `PIPE_LANG=en` instalowane sa angielskie wersje: `update-container`, `harden-ssh`, `free-disk-space`
(pozostale cztery maja te sama nazwe w obu jezykach).

---

## Pamiec incydentow

Kazdy rozwiazany alert zostaje w `backend/data/incidents.json`: co sie stalo, kiedy i jak dlugo, co ustalil agent
po *Zbadaj* (albo worker przy alercie z webhooka) i co pomoglo -- zmiany z dziennika wykonane w czasie trwania
problemu. Bez dodatkowych zapytan do LLM.

Gdy ten sam problem wraca, alert na Telegramie ma linie **Poprzednio: 2026-09-12 (40 min) — ustalenia: logi
nginx bez rotacji... — pomoglo: journalctl --vacuum-size=200M**, a polecenie *Zbadaj* dostaje te historie: agent
najpierw sprawdza, czy to ta sama przyczyna, i proponuje sprawdzona naprawe (dalej do zatwierdzenia).
`/incydenty` pokazuje historie; agent siega po nia sam (*"czy to juz sie zdarzalo?"*).

---

## Alerty z zewnatrz (webhooki)

Pipe dolacza do monitoringu, ktory juz masz, zamiast go zastepowac:

```bash
# backend/.env
WEBHOOK_PORT=7380
WEBHOOK_TOKEN=$(openssl rand -hex 24)
```

| Zrodlo | Adres | Konfiguracja po stronie nadawcy |
|--------|-------|---------------------------------|
| Prometheus Alertmanager | `/hook/alertmanager` | `webhook_configs: - url: http://127.0.0.1:7380/hook/alertmanager` + `http_config.authorization.credentials: <token>` |
| Grafana | `/hook/grafana` | contact point typu Webhook, naglowek `Authorization: Bearer <token>` |
| Uptime Kuma | `/hook/uptime-kuma?token=<token>` | powiadomienie typu Webhook (application/json) |
| GitHub | `/hook/github` | webhook repozytorium, *Secret* = token; zdarzenia *Workflow runs*, *Deployment statuses* |
| Wszystko inne | `/hook/generic` | `{"title", "message", "severity", "status": "firing|resolved", "name"}` |

Alert trafia do czuwania (znika po `resolved`) i na Telegram z przyciskiem *Zbadaj*. Z `WEBHOOK_INVESTIGATE=1`
nowy alert od razu bada worker (tylko odczyty: uslugi, kontenery, logi, zasoby, ostatnie zmiany) i raport
przychodzi chwile po alercie -- ten sam alert najwyzej raz na godzine, 10 badan dziennie.

W Dockerze port jest publikowany tylko na `127.0.0.1:7380`; nadawcy spoza serwera -- przez odwrotne proxy z TLS.

---

## Wiadomosci glosowe

Na Telegramie wystarczy nagrac wiadomosc: *"sprawdz, czemu sklep nie dziala"*. Bot odpisze *Uslyszalem: ...*
i przekaze tekst agentowi. Transkrypcja: endpoint Whisper zgodny z OpenAI -- przy `LLM_PROVIDER=openai` albo
`groq` dziala bez konfiguracji; przy innych providerach ustaw `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL`
(np. darmowy Groq `whisper-large-v3-turbo` albo lokalny faster-whisper). Nagranie OGG idzie bez konwersji.

---

## Role i wielu uzytkownikow

| Rola | Moze | Nie moze |
|------|------|----------|
| admin | wszystko | -- |
| viewer | rozmowa, diagnoza, odczyty, wykresy, raporty, audyt, alerty | zatwierdzac zmian, `/cofnij`, zapisywac pamieci Pipe (SERVER.md, skille, cele, rutyny) |

- Telegram: `TELEGRAM_ALLOWED_USER_IDS` (admini) i `TELEGRAM_VIEWER_IDS` (tylko odczyt, z `AGENT_VIEWER_TOKEN`).
- CLI i inne klienty: osobny token na osobe albo laptop, odwolywalny:
  `python3 -m backend.tokens add laptop-kuba --role admin`, `list`, `revoke laptop-kuba`.
- Role egzekwuje backend. Sesja nalezy do tokenu, ktory ja zalozyl.

---

## Jezyk: polski albo angielski

`PIPE_LANG=pl` (domyslnie) albo `PIPE_LANG=en` przelacza caly produkt, nie tylko jezyk odpowiedzi modelu:

| Warstwa | Co sie zmienia |
|---------|----------------|
| Agent | system prompt, opisy 23 narzedzi, prompty workerow, rutyn, skanu serwera i VIBE |
| Komunikaty | pytania o potwierdzenie, plan bezpiecznika, wyniki narzedzi, bledy walidacji |
| Czuwanie | tytuly alertow, poranny raport, `/zmiany`, `/zdrowie`, `/audyt` z poprawkami, powitanie |
| MCP | tytuly i opisy narzedzi serwera MCP, instrukcje dla zewnetrznego agenta |
| Klienci | CLI (`--lang en`), bot Telegrama (menu `/`, przyciski, etykiety), kreator i instalator |
| Skille | wbudowane skille w wersji angielskiej |

Znaczniki protokolu -- `[POTWIERDZ]`, `[BLAD]`, `[ODMOWA]`, `[PAMIEC]`, `[ZREDAGOWANO: ...]` -- sa takie same
w obu jezykach: serwer rozpoznaje po nich status ramki, a klienci zamieniaja je na etykiety w swoim jezyku.
Komendy maja angielskie aliasy (`/report`, `/changes`, `/undo`, ...), ktore dzialaja obok polskich.

Dane zapisane wczesniej (SERVER.md, VIBE, wlasne skille, wpisy dziennika) zostaja w jezyku, w ktorym powstaly.

---

## MCP -- Pipe jako brama dla innych agentow i klient cudzych serwerow

### Pipe jako serwer MCP

Claude Code, Cursor albo wlasny agent dostaje narzedzia Pipe zamiast golej powloki na produkcji:

| Narzedzie | Co robi |
|-----------|---------|
| `run_command` | komenda przez klasyfikator Pipe: odczyt od razu; zmiana -> **zgoda administratora**, potem bezpiecznik i dziennik; zakazana -> odmowa |
| `get_approval` | stan zgody i wynik (tylko dla agenta, ktory o nia prosil) |
| `read_file` | plik hosta; pliki z sekretami nie sa wydawane, sekrety w tresci -- redagowane |
| `server_status`, `server_changes`, `infra_map`, `health_checks`, `security_audit`, `journal` | wiedza Pipe o serwerze |
| `ask_pipe` | pytanie do agenta Pipe (zna SERVER.md, DIRECTORY, historie) -- w roli tylko do odczytu |

Zgoda przychodzi na Telegram z komenda i planem bezpiecznika (kopia, `nginx -t`, weryfikacja) oraz przyciskami
*Zatwierdz* / *Odrzuc*; w CLI -- `/zgody`. Agent dowiaduje sie o wyniku przez `get_approval`.

**Polaczenie z laptopa** -- most stdio w CLI, przez ten sam tunel SSH i tokeny:

```json
{"mcpServers": {"pipe": {"command": "pipe", "args": ["--mcp", "--host", "root@serwer"],
                         "env": {"AGENT_TOKEN": "<token>"}}}}
```

Osobny token dla agenta: `python3 -m backend.tokens add claude-code --role admin` (albo `--role viewer`:
agent tylko czyta, nie moze prosic o zgody). **Na serwerze** -- endpoint Streamable HTTP: `MCP_PORT=7381`,
`http://127.0.0.1:7381/mcp`, token w `Authorization: Bearer`.

Obslugiwane wersje protokolu: 2026-07-28 (`server/discover`, wersja w `_meta`, naglowki `Mcp-Method`/`Mcp-Name`)
i starsze z `initialize` (2025-11-25, 2025-06-18, 2025-03-26).

### Pipe jako klient MCP

Narzedzia innych serwerow MCP staja sie narzedziami agenta (`mcp__github__create_issue`...). Dodasz je zdaniem --
*"dodaj serwer MCP github: npx -y @modelcontextprotocol/server-github, token w GITHUB_PERSONAL_ACCESS_TOKEN,
bez pytania get_* i list_*"* -- albo w `backend/data/mcp.json`:

```json
{"servers": {
  "github":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
              "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}, "autoApprove": ["get_*", "list_*", "search_*"]},
  "grafana": {"url": "http://127.0.0.1:8000/mcp", "headers": {"Authorization": "Bearer ..."}, "trustReadOnly": true}
}}
```

- Kazde wywolanie wymaga TAK z widocznymi argumentami, chyba ze narzedzie pasuje do `autoApprove` albo serwer ma
  `trustReadOnly`, a narzedzie deklaruje `readOnlyHint`. Zatwierdzone wywolanie trafia do dziennika zmian.
- Dodanie serwera zawsze wymaga potwierdzenia. Podproces stdio nie dostaje klucza LLM ani tokenow Pipe.
- stdio i Streamable HTTP; wersja protokolu wykrywana sama (2026-07-28 albo `initialize`).
- `/mcp` pokazuje serwery i ich stan; *"polacz ponownie serwery MCP"* -- `mcp_manage reload`.
