# Workery, cele, rutyny, czuwanie, diagramy i historia serwera -- Pipe

Pipe v0.46.0

Ten dokument opisuje funkcje, ktore odrozniaja Pipe od agentow ogolnego przeznaczenia:
agent, ktory sam pilnuje serwera, widzi jego architekture i zarzadza wieloma maszynami naraz.

---

## Pipe pisze pierwszy

Nie musisz pytac, co sie dzieje. Pipe pisze sam -- na Telegram, w `pipe web` i w CLI -- gdy cos wymaga uwagi,
i od razu daje przycisk, ktory sprawe zalatwia. Wszystko, co wysle, trafia tez do **skrzynki**
(`backend/data/feed.json`), wiec nic nie ginie, gdy nikt nie byl podlaczony.

| Wiadomosc | Kiedy | Przyciski |
|-----------|-------|-----------|
| Alert | problem juz jest: dysk, pamiec, kontener, strona, certyfikat, podatnosci | *Zwolnij miejsce*, *Zalataj*, *Zbadaj* |
| Ustalenia | krytyczny problem: Pipe sam sprawdza przyczyne (worker, tylko odczyty) i pisze, co ustalil | *Napraw* -- jedno potwierdzenie |
| Podpowiedz | problem dopiero bedzie albo cos mozna poprawic (tabela nizej) | akcja, *Pozniej*, *Mniej takich* |
| Raport poranny | o `DIGEST_TIME` | -- |
| Rozwiazane | problem minal | -- |

**Podpowiedzi** licza sie bez modelu jezykowego, z danych, ktore Pipe i tak zbiera (co `INSIGHTS_INTERVAL`, 1 h):

| Podpowiedz | Skad |
|------------|------|
| *Dysk / zapelni sie za ok. 4 dni* | tempo przyrostu z historii pomiarow (regresja z ostatnich 48 h) |
| *Moge zwolnic ok. 6 GB* | obrazy Dockera bez tagu, cache budowania, pobrane paczki apt |
| *Kontener X zrestartowal sie 4 razy w ciagu doby* | licznik restartow (petle restartow lapie alert) |
| *Serwer od 5 dni czeka na restart* | `/run/reboot-required` |
| *Pamiec RAM stale rosnie* | zajetosc RAM rosnaca przez dobe (mozliwy wyciek) |

**Samodzielne badanie.** Gdy pojawia sie krytyczny problem (kontener przestal dzialac, strona nie odpowiada, dysk
pelny), Pipe nie czeka na *Zbadaj*: worker sprawdza logi, zasoby i zmiany z ostatniej doby (tylko odczyty), a ustalenia
przychodza zaraz po alercie, z przyciskiem *Napraw*. `AUTO_INVESTIGATE=critical` (domyslnie), `all` albo `off`;
najwyzej `AUTO_INVESTIGATE_DAILY` (4) badan dziennie, ten sam problem najwyzej raz na 6 h.

**Kiedy powiadamiac.** Poziom (`NOTIFY_LEVEL`): `all` -- wszystko; `important` (domyslnie) -- problemy, ostrzezenia,
raport i wazne podpowiedzi, informacyjne czekaja w skrzynce; `critical` -- tylko to, co nie moze czekac. Cisza nocna
(`QUIET_HOURS=22:00-07:00`): w nocy przychodza tylko sprawy krytyczne, reszta rano razem z raportem. Najwyzej
`INSIGHTS_PER_DAY` (3) podpowiedzi dziennie z powiadomieniem. *Mniej takich* wycisza dany rodzaj podpowiedzi (zostaje w
skrzynce bez powiadomienia), *Pozniej* odklada ja o dobe. Te same ustawienia zmienisz w trakcie pracy:
`/powiadomienia` (Telegram -- przyciskami, CLI -- `/powiadomienia wazne cisza 22-7 dziennie 3`, `pipe web` -- dzwonek na
pulpicie).

- `/nowosci` (`/news`): skrzynka z akcjami (w CLI wpisujesz np. `1a`). Krotki stan bez modelu: `/status`.
- Przyciski buduje backend -- klient odsyla tylko numer akcji (`feed_act`). *Napraw* i *Zalataj* sa dla administratora.
- Klienci powiadamiaja tylko o wiadomosciach z `push` (poziom, cisza nocna, limit); CLI pokazuje je nad promptem,
  `pipe web` jako okienko, Telegram jako wiadomosc.

---

## Warsztat: projekty, ktore Pipe stawia sam

Napisz *"postaw mi te aplikacje: https://github.com/acme/notatnik"* albo *"napisz skrypt, ktory policzy zamowienia z
logow"*, a Pipe poprowadzi to do konca sam. Projekty leza w jednym katalogu hosta -- **warsztacie**
(`WORKSPACE_DIR`, domyslnie `/root/pipe-workspace`), kazdy w swoim podkatalogu i w `backend/data/projects.json`.

| Krok | Co robi Pipe | Pyta? |
|------|--------------|-------|
| `clone` | `git clone --depth 1` z adresu https albo `git@` (klucz SSH serwera); bez hookow, submodulow, LFS; za duze repozytorium (`PROJECT_MAX_MB`) usuwa | nie |
| `inspect` | co to jest i jak to uruchomic -- bez modelu: compose, Dockerfile, Node, Python (Django, FastAPI, Flask, Streamlit), Go, PHP, strona statyczna; porty, potrzebne zmienne, README i AGENTS.md | nie |
| zapis plikow | `write_file` w katalogu warsztatu (np. Dockerfile, skrypt) | nie |
| `exec` | komenda w jednorazowym kontenerze: tylko katalog projektu jako `/work`, bez `docker.sock` i uprawnien (`--cap-drop ALL`, `no-new-privileges`), limit pamieci, procesow i czasu | nie* |
| `run` | usluga na stale: kontener `pipe-<nazwa>` (albo projekt compose `pipe-<nazwa>`), port tylko na `127.0.0.1` | **tak, raz** -- z planem |
| `logs`, `stop`, `start`, `pull` | logi, zatrzymanie, wznowienie, `git pull --ff-only` | nie |
| `remove` | katalog, kontenery i zbudowany obraz | **tak** |

\* Po przeczytaniu tresci z zewnatrz (README sklonowanego repozytorium, strona z internetu) Pipe pyta o zgode na
`exec` do Twojej nastepnej wiadomosci -- jak YOLO: tekst z cudzego repozytorium nie moze sam uruchomic kodu.

- Kod z repozytorium nigdy nie dziala w procesie ani kontenerze Pipe (tam jest `docker.sock`). Instaluje sie i
  uruchamia w kontenerach -- na serwerze nie przybywa Node.js, Pythona ani bibliotek.
- `run` jest w dzienniku zmian: `/cofnij` zatrzymuje i usuwa wdrozenie. Ponowne `run` zostaje na tym samym porcie.
- Projekt compose z ryzykownymi ustawieniami (`privileged`, `docker.sock`, siec hosta, montowanie `/etc`) albo
  portami otwartymi na swiat -- potwierdzenie mowi to wprost.
- Gdy `inspect` nie wie, jak uruchomic projekt, agent pisze Dockerfile w jego katalogu i uruchamia `run strategy=dockerfile`.
- Adres aplikacji to `http://127.0.0.1:<port>` na serwerze (z laptopa: `ssh -L <port>:127.0.0.1:<port> <serwer>`);
  wystawienie pod domena przez reverse proxy to osobna prosba (zwykle potwierdzenie).
- `/projekty` (`/projects`) w CLI i na Telegramie, karty projektow z logami i zatrzymaniem na pulpicie `pipe web`.
  `WORKSPACE_AUTONOMY=0` -- zgoda na kazda operacje. Tryb obserwacji i rola viewer -- tylko lista i logi.

---

## Oszczednosc tokenow

Czuwanie, podpowiedzi, raporty, `/status`, `/zmiany`, `/audyt`, `/sprzataj` i pulpit dzialaja bez modelu
jezykowego -- zero tokenow. Gdy rozmowa juz idzie do modelu, Pipe wysyla mniej:

- **Narzedzia na zadanie** (`TOOLS_MODE=lazy`). Opisy 28 narzedzi to ~6 tys. tokenow przy kazdym kroku agenta.
  Rozmowa zaczyna od podstawowych (~2,1 tys.), a reszta wlacza sie sama, gdy wiadomosc jej potrzebuje
  (*"przypomnij jutro"* -> przypomnienia, *"czy serwer jest bezpieczny"* -> audyt) albo gdy model poprosi
  (`load_tools`). Wlaczona grupa zostaje do konca rozmowy, wiec cache promptu u providera dziala dalej.
  `TOOLS_MODE=all` -- wszystkie zawsze.
- **Skracanie starych wynikow** (`CONTEXT_KEEP_TURNS`, 2). Wyniki narzedzi starsze niz dwie tury ida do modelu jako
  poczatek z adnotacja (*"jesli potrzebujesz, uruchom jeszcze raz"*). Badania JetBrains (*The Complexity Trap*, NeurIPS
  2025) pokazaly, ze taki zabieg obniza koszt agenta o ponad polowe bez spadku skutecznosci. Historia w sesji zostaje pelna.
- **Krotszy prompt**: bez powtorzonej listy narzedzi i bez obowiazkowego prefiksu z katalogiem w kazdej odpowiedzi.
- `/koszt` i pulpit pokazuja, ile tokenow Pipe **nie wyslal** (narzedzia na zadanie, skrocone wyniki) i ile wejscia
  przyszlo z cache providera.

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

> Eksperymentalne: dziala i ma testy, ale ma za soba mniej uzycia na prawdziwych serwerach niz reszta Pipe.
> Jesli cos nie gra, zglos to w issue.

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

> Eksperymentalne: dziala i ma testy, ale ma za soba mniej uzycia na prawdziwych serwerach niz reszta Pipe.
> Jesli cos nie gra, zglos to w issue.

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
- CLI nie subskrybuje zdarzen, ale czekajac na wpis co 10 s odbiera przypomnienia (takze wynik `/aktualizuj`) i pokazuje
  je nad promptem. Przypomnienie ustawione z CLI idzie do subskrybentow (Telegram, `pipe web`) i dodatkowo czeka na odbior w CLI.
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
- Alert ma przyciski (**Zbadaj**, przy dysku **Zwolnij miejsce**, przy podatnosciach **Zalataj**); krytyczny Pipe
  bada sam i dosyla ustalenia (sekcja *Pipe pisze pierwszy*).
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
- **Podatnosci** -- ile pakietow serwera ma poprawke (w tym pilnych i aktywnie wykorzystywanych), trzy najgrozniejsze
  z opisem, czy serwer czeka na restart, zdalne serwery, obrazy i aplikacje z poprawkami; na koncu `/podatnosci`
  i `/zalataj`
- **Rutyny** -- ostatni status kazdej rutyny
- **LLM** -- zuzycie tokenow z poprzedniego dnia

Do raportu dolaczony jest wykres obciazenia z ostatniej doby. `/raport` -- raport na zadanie.

---

## Koszty LLM

Kazde zapytanie do providera (rozmowa, workery, rutyny, VIBE) jest liczone w `backend/data/usage.json`.

- `/koszt` -- tokeny dzis (z podzialem na uzytkownika, workery, rutyny, VIBE i model), ostatnie dni, miesiac, oraz ile
  tokenow Pipe nie wyslal i ile wejscia przyszlo z cache (sekcja *Oszczednosc tokenow*).
- Koszt w USD pojawi sie po podaniu cen modelu: `LLM_PRICE_IN` i `LLM_PRICE_OUT` (USD za milion tokenow),
  osobno `WORKER_PRICE_IN/OUT` dla `WORKER_MODEL`.
- `DAILY_TOKEN_LIMIT` / `DAILY_COST_LIMIT` -- po przekroczeniu agent odpowiada komunikatem o limicie do polnocy
  i nie wysyla zapytan do providera. Czuwanie, raport i komendy bez LLM (`/mapa`, `/zmiany`, `/wykres`) dzialaja dalej.

---

## Zmiany z bezpiecznikiem i `/cofnij`

Kazda operacja, ktora zatwierdzasz, dostaje **plan** -- widoczny w potwierdzeniu, zanim nacisniesz TAK:

```
Potrzebuje Twojej zgody
$ sed -i 's/8080/8081/' /etc/nginx/sites-enabled/shop && systemctl reload nginx
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
| Podatnosci | znane podatnosci z poprawka w pakietach systemu, obrazach kontenerow, zdalnych serwerach i aplikacjach (OSV.dev, CISA KEV) -- ponizej |
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
SUID albo `ld.so.preload` -- alert krytyczny. Zmiany zrobione przez Pipe (sa w dzienniku) nie alarmuja. Aktualizacje
pakietow tez nie: program SUID albo konfiguracja, ktorych tresc zgadza sie z suma z bazy pakietow (dpkg `md5sums`
i `Conffiles`, apk), to latka, a nie podmiana. Binarka podmieniona po instalacji dalej daje alert.

**Log SSH** (`/var/log/auth.log` albo `/var/log/secure`, czytany przyrostowo): seria nieudanych logowan,
udane logowanie haslem z adresu, ktory wczesniej zgadywal hasla, logowanie z nowego adresu.

**Podatnosci w pakietach** (Debian, Ubuntu, Alpine). Pipe czyta liste pakietow z bazy dpkg/apk (niczego nie
uruchamia) i pyta [OSV.dev](https://osv.dev) o znane podatnosci dla dokladnych wersji. Liczy sie tylko to, co ma
poprawke -- podatnosci bez poprawki Pipe pomija, bo zwykly Debian ma ich setki i nic sie z nimi nie da zrobic.
Raport zaczyna sie od "Co z tym zrobic": jedna linia na serwer, obrazy i aplikacje z konkretnym krokiem. Do OSV trafiaja wylacznie
nazwy i wersje pakietow; `VULN_INTERVAL=0` wylacza skan.

- Kiedy: gdy zmieni sie lista pakietow (nowa aplikacja, aktualizacja -- wtedy OSV dostaje tylko zmienione pakiety)
  i co `VULN_INTERVAL` (6 h) dla wszystkich, bo nowe podatnosci pojawiaja sie w starych wersjach. `/podatnosci`,
  `/audyt` i `/zalataj` czytaja liste pakietow, obrazow i zaleznosci od nowa przy kazdym wywolaniu: zmienione
  ida do OSV od razu, calosc najwyzej co 10 minut, wiec to zwykle kilka sekund. Gdy skan trwa dluzej niz 45 s, widac
  ostatni wynik z godzina, z ktorej pochodzi.
- Alert przychodzi tylko o **nowych pilnych**: aktywnie wykorzystywanych (katalog CISA KEV) albo krytycznych
  i wysokich. Alert przychodzi sam (Telegram, `pipe web`) i przy kazdej podatnosci mowi zwyklymi slowami, czego
  dotyczy i co grozi, a numer CVE jest na koncu w nawiasie. Pipe najpierw czeka (do 2 minut) na wyjasnienia modelu,
  bez nich wysyla opis z OSV. Znane podatnosci nie wracaja po restarcie Pipe. Na koncu alertu jest to, co zrobi
  przycisk *Zalataj*, i czego nie zalata: poprawek tylko z Ubuntu Pro, kontenerow bez compose i kontenerow samego
  Pipe (te odswieza `/aktualizuj`). Reszta jest w porannym raporcie i w `/audyt`; pytanie do agenta o podatnosci tez
  daje opis kazdej z nich.
- W `/audyt` podatnosci sa najwyzej trzema krotkimi punktami (KEV, krytyczne i wysokie, pozostale) z nazwami
  pakietow i odeslaniem do `/zalataj`, ktore podbija tylko te pakiety. Punkt o aktywnie wykorzystywanych jest
  pierwszy, a pakiet, ktorego `/zalataj` teraz nie zainstaluje, ma przy nazwie powod (*tylko Ubuntu Pro*, *bez zmian
  po ostatniej latce*). Czego nie dalo sie sprawdzic (np. zdalny serwer bez polaczenia), jest w linii *Nie
  sprawdzono*. Pod audytem przychodzi osobna wiadomosc z podatnosciami: kazdy pakiet z opisem najpowazniejszej.
  Alert i raport zawsze mieszcza sie w jednej wiadomosci Telegrama, a reszta jest policzona. W trybie docker Pipe
  instaluje latki na hoscie przez kontener pomocniczy (patrz *Komendy na hoscie* w bezpieczenstwie); w Kubernetesie
  latki wezla instalujesz w jego powloce.
- **`/zalataj` lata od razu.** Samo `/zalataj` (albo przycisk *Zalataj* przy alercie w Telegramie i `pipe web`) to
  zgoda na cala latke: Pipe nie pokazuje komend i nie pyta o kazda z osobna. Kolejno instaluje poprawki pakietow tego
  serwera, odswieza obrazy kontenerow z compose i lata zdalne serwery, z krokami na zywo. `/zalataj obrazy` robi tylko
  obrazy, `/zalataj serwer` tylko pakiety tego serwera, `/zalataj serwery` tylko zdalne serwery, `/zalataj web2` jeden
  z nich. Komendy sklada serwer z biezacego skanu, bez LLM. Kazda czesc idzie przez bezpiecznik (strony przed i po,
  wpis w dzienniku); gdy weryfikacja po zmianie nie przejdzie, kolejnych czesci Pipe nie lata. Potem skanuje od nowa
  i mowi, co zalatal, co zostalo bez zmian i dlaczego, oraz co zostaje do zrobienia recznie. Latanie dziala w tle:
  zamkniecie klienta go nie przerywa, a wynik przychodzi wtedy jako wiadomosc.
- Apt dostaje komplet porzadkow: najpierw dokonczenie przerwanej instalacji (`dpkg --configure -a`), czekanie na
  blokade zamiast bledu, gdy akurat dziala unattended-upgrades, i usuniecie pobranych paczek oraz starych jader, ktore
  apt sam uznaje za zbedne (zostaja dzialajace i najnowsze, wiec `/boot` sie nie zapelnia). Potem `--only-upgrade`
  tylko podatnych pakietow i to samo sprzatanie jeszcze raz. Jadro Pipe podbija przez metapakiet (`linux-image-generic`,
  `linux-image-amd64`...), bo nowe jadro to nowy pakiet. Po latce sprzata tez obrazy bez tagu, cache budowania starszy
  niz doba oraz cache npm, pip, yarn i composer, a wynik podaje, ile miejsca zwolnil.
- Pakietu, ktorego apt nie podbil, i obrazu, ktorego nic nie zmienilo, Pipe nie probuje znowu przez dobe; raport mowi
  dlaczego. Poprawek tylko z Ubuntu Pro (ESM) nie probuje wcale, dopoki subskrypcja nie jest wlaczona (`pro attach`).
  Kontenerow samego Pipe `/zalataj` nie odtwarza (to robi `/aktualizuj`).
- Pakietow systemowych nie da sie wiarygodnie cofnac (starej wersji czesto nie ma juz w repozytorium), wiec
  `/cofnij` ich nie obejmuje. Biblioteki i jadro dzialaja po restarcie uslug albo serwera: gdy system czeka na
  restart, raport to mowi.
- Jadro ma tysiace rekordow CVE, wiec dla niego Pipe bierze tylko biuletyny dystrybucji (USN, DSA, DLA). Liczy sie
  najnowsze zainstalowane jadro: stare zostaja do autoremove i nie sa juz zglaszane. Pierwszy skan trwa okolo
  minuty, kolejne korzystaja z zapisanych szczegolow.
- Agent w rozmowie lata tym samym narzedziem (`security_patch`): na "zalataj serwer" dostajesz jedno potwierdzenie
  z lista tego, co zostanie zalatane, zamiast pytania o kazda komende apt i docker.
- **Obrazy kontenerow.** Ten sam skan obejmuje pakiety w obrazach dzialajacych kontenerow (Debian, Ubuntu, Alpine):
  Pipe czyta ich baze pakietow przez `/proc/<pid>/root` kontenera albo z warstw overlay, niczego w kontenerze nie
  uruchamiajac. Jeden wynik na obraz, alert `vuln:images`. Obrazy bez systemu (distroless, scratch) sa wymienione
  jako niesprawdzone. `/zalataj obrazy` najpierw zostawia poprzedni obraz pod tagiem `pipe-rollback-*` (sprzatanie go
  nie usunie, a `/cofnij` do niego wraca), potem pobiera nowsze obrazy z rejestru pod tym samym tagiem
  (`pull --ignore-buildable`) i przebudowuje uslugi z `build:` na aktualnym obrazie bazowym (`build --pull`). Gdy
  `FROM` w Dockerfile takiej uslugi ma przypieta wersje, a w tej samej linii jest nowsza (`node:20.11-slim` ->
  `node:20.20-slim`, `postgres:15.4` -> `postgres:15.19`; zmienia sie tylko ostatnia liczba, nigdy wersja glowna),
  Pipe najpierw podbija ten tag (tagi z Docker Hub, kopia Dockerfile w dzienniku -- `/cofnij` ja przywraca). Tag
  plywajacy (`node:20`), obraz przypiety skrotem, `FROM` ze zmienna i rejestry inne niz Docker Hub zostaja bez zmian. Obraz,
  ktory nie ma nowszej wersji (przypiety tag, obraz bazowy jeszcze nieprzebudowany), dostaje poprawki z repozytorium
  swojego systemu: kontener tymczasowy z tego obrazu instaluje je jako root, a `docker commit` zapisuje wynik pod tym
  samym tagiem z jego uzytkownikiem, ENTRYPOINT i CMD (jak `copa patch`). Potem `up -d` tylko tych uslug i weryfikacja
  kontenerow projektu. Kontenery bez compose dostaja tylko podpowiedz.
- **Zdalne serwery.** Cele ssh z `target_manage` sa w tym samym skanie: Pipe czyta ich baze pakietow jednym
  polaczeniem ssh (os-release, dpkg/apk, reboot-required), a `/zalataj` instaluje na nich poprawki tymi samymi
  krokami apt, jako root albo przez `sudo -n` (sudo z haslem nie zadziala, wynik mowi, co dodac w sudoers). Serwer bez
  polaczenia jest wymieniony w raporcie, alert `vuln:servers` przychodzi o nowych pilnych.
- **Zaleznosci aplikacji.** Pipe szuka lockfile'i w katalogach z DIRECTORY, w katalogach projektow compose
  dzialajacych kontenerow i w repozytoriach git pod `/root`, `/home`, `/srv`, `/opt`, `/var/www` i `/data`, razem
  z podkatalogami (np. `backend/`, `frontend/`), bez `node_modules`, `vendor` i katalogow budowania. Czyta
  `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `requirements*.txt` (tylko `==`), `poetry.lock`, `Pipfile.lock`,
  `uv.lock`, `go.mod`, `Cargo.lock`, `composer.lock`, `Gemfile.lock`. Wynik jest w `/audyt` z wersja, ktora usuwa
  podatnosci; alert `vuln:apps` tylko dla aktywnie wykorzystywanych i krytycznych, z przyciskiem *Zalataj*.
- **Latki zaleznosci aplikacji** (`/zalataj`, samo to: `/zalataj aplikacje`). Pipe podbija podatne pakiety w plikach
  zaleznosci menedzerem danego jezyka, uruchomionym w jednorazowym kontenerze (tylko katalog aplikacji, bez
  `docker.sock` i uprawnien, jako wlasciciel katalogu, bez skryptow instalacyjnych pakietow):

  | Plik | Co robi Pipe |
  |------|--------------|
  | `package-lock.json` | zaleznosci z `package.json`: `npm install <pakiet>@<wersja> --package-lock-only` (dokladne przypiecie zostaje dokladne); przechodnie: `npm audit fix --package-lock-only` |
  | `yarn.lock` (yarn 2+), `pnpm-lock.yaml` | `yarn up` / `pnpm update` tylko w pliku blokady |
  | `requirements*.txt` | nowa wersja po `==` w tej samej linii (plik z `--hash` zostaje -- hashe by nie pasowaly) |
  | `poetry.lock`, `uv.lock` | `poetry update --lock`, `uv lock --upgrade-package` |
  | `go.mod`, `Cargo.lock` | `go get <modul>@v<wersja>` + `go mod tidy`, `cargo update -p --precise` |
  | `composer.lock`, `Gemfile.lock` | `composer update --no-install --no-scripts --no-plugins`, `bundle lock --update` |

  Kopia zmienianych plikow trafia do dziennika (jeden wpis na aplikacje), a gdy z katalogu buduje sie projekt compose,
  Pipe robi `build` i `up -d` z weryfikacja kontenerow -- `/cofnij` przywraca pliki i przebudowuje projekt. Czego
  menedzer nie podbil (ograniczenie wersji w `package.json`, `pyproject.toml`, `composer.json`...), pokazuje wynik i
  `/podatnosci`; Pipe nie probuje tego znowu przez dobe. Recznie zostaja: yarn 1 (nie umie zmienic samego
  `yarn.lock`), `Pipfile.lock` i wersja samego Go.
- **`/podatnosci`** pokazuje wszystko w jednym miejscu: pakiet pogrubiony, wersja i wersja z poprawka,
  pod nim podatnosci z waga i krotkim wyjasnieniem w jezyku Pipe -- co atakujacy moze zrobic i kiedy to grozi.
  Wyjasnienia pisze model (`WORKER_MODEL`) na podstawie opisu z OSV, raz na podatnosc, i Pipe je zapamietuje;
  bez modelu widac oryginalny opis z OSV. W `pipe web` raport jest w kartach (serwer, zdalne serwery, obrazy,
  aplikacje) z jednym przyciskiem *Zalataj*, a latanie pokazuje kroki na zywo i od razu stan po latce.

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

**Skille i pluginy z innych ekosystemow.** `/pobierz skill <wlasciciel/repo|URL> [filtr]` i
`/pobierz plugin <...>` (`/download`) biora gotowe zestawy z repozytorium git -- ten sam format `SKILL.md` co Claude
Code, a pluginy w ukladzie pluginow Claude Code (`skills/`, `.mcp.json`, marketplace). Najpierw podglad: ktore skille
trafia do pamieci (sam tekst, skrypty sa pomijane), ktore serwery MCP dojda (w izolowanym kontenerze, sekrety do
dopisania w `backend/.env` jako `MCP_...`) i co zostanie pominiete -- z powodem (za dlugi skill, serwer, ktory
potrzebuje plikow pluginu, haki, agenty). Instalacja dopiero po *Zainstaluj*. Ta sama nazwa w kilku pluginach
dostaje przedrostek pluginu (`telegram-access`). W rozmowie: *"pobierz skille z anthropics/skills"*.

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
nowy alert od razu bada worker (tylko odczyty: uslugi, kontenery, logi, zasoby, zmiany z ostatniej doby), tak samo
jak problemy wykryte przez Pipe: ustalenia przychodza chwile po alercie z przyciskiem *Napraw* -- ten sam alert
najwyzej raz na 6 h, 10 badan z webhookow dziennie (osobno od `AUTO_INVESTIGATE_DAILY`).

W Dockerze port jest publikowany tylko na `127.0.0.1:7380`; nadawcy spoza serwera -- przez odwrotne proxy z TLS.

---

## Zalaczniki: pliki, zrzuty ekranu, logi

Agentowi mozna wyslac plik razem z pytaniem: zrzut ekranu z bledem, log, konfiguracje nginx. Na Telegramie
zdjecie albo dokument (podpis to pytanie, album kilku zdjec idzie jako jedna wiadomosc), w `pipe web` spinacz,
przeciagniecie pliku na okno albo wklejenie zrzutu ze schowka (miniatury przed wyslaniem), w CLI `/plik <sciezka>
[pytanie]` albo `@sciezka` w zwyklej wiadomosci (Tab podpowiada sciezki).

Backend (`core/attachments.py`) rozpoznaje plik po zawartosci, nie po nazwie:

- obraz (PNG, JPEG, GIF, WebP) idzie do modelu jako obraz i zostaje w historii tylko na jedna ture -- potem
  zastepuje go znacznik, zeby kolejne pytania nie wysylaly go od nowa. Gdy model nie przyjmuje obrazow (np. czesc
  modeli Ollamy), Pipe mowi to wprost: *model nie widzi obrazow* i odpowiada bez obrazu,
- tekst (UTF-8) jest wklejany do wiadomosci po redakcji sekretow i przycieciu do 24 tys. znakow,
- reszta (archiwa, binarki, PDF) trafia do `DATA_DIR/uploads/` (0600, sprzatane po 7 dniach), a agent dostaje
  sciezke; przeniesienie pliku gdzie indziej to zwykla zmiana z potwierdzeniem.

Limity: 10 MB na plik, 20 MB i 10 plikow na wiadomosc. Tresc zalacznika to dane, nie polecenia: tryb YOLO jest
wstrzymany do nastepnej wiadomosci, tak jak po przeczytaniu strony z internetu.

## Wiadomosci glosowe

> Eksperymentalne: dziala i ma testy, ale ma za soba mniej uzycia na prawdziwych serwerach niz reszta Pipe.
> Jesli cos nie gra, zglos to w issue.

Na Telegramie wystarczy nagrac wiadomosc: *"sprawdz, czemu sklep nie dziala"*. Bot odpisze *Uslyszalem: ...*
i przekaze tekst agentowi. W `pipe web` jest przycisk mikrofonu: nagranie (do 5 minut) przegladarka zamienia na
WAV 16 kHz i wysyla do transkrypcji, a tekst idzie do agenta. Transkrypcja: przy `LLM_PROVIDER=gemini` robi ja
sam model Gemini (wejscie audio), przy `openai` albo `groq` endpoint Whisper tego providera -- w obu przypadkach
bez konfiguracji. Przy innych providerach ustaw `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` (np. darmowy Groq
`whisper-large-v3-turbo` albo lokalny faster-whisper); `STT_*` ma zawsze pierwszenstwo.

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

## Interfejs webowy i schemat na zywo

`pipe web` otwiera w przegladarce interfejs z rozmowa i panelem bocznym. Dziala na Twoim komputerze, przez ten sam
tunel co CLI.

**Pulpit** (pierwsza zakladka panelu) odpowiada na pytanie *"czy wszystko dziala?"* bez modelu: naglowek zwyklymi
slowami (*"Wszystko dziala"* albo *"Wymaga uwagi: ..."*), pierscienie procesora, pamieci i dysku z przebiegiem
ostatniej doby, skrzynka *Od Pipe* (wiadomosci z przyciskami akcji, nowe oznaczone kropka; dzwonek w gornym pasku
pokazuje, ile ich jest), karty projektow z warsztatu (logi, zatrzymanie, *Z GitHuba*) i zuzycie modelu dzis (tokeny,
koszt, cache, ile Pipe zaoszczedzil). Dzwonek przy skrzynce otwiera ustawienia powiadomien. Pusty ekran rozmowy
podsuwa zadania zwyklymi slowami (*Czy wszystko dziala?*, *Zwolnij miejsce na dysku*, *Uruchom aplikacje z GitHuba*,
*Napisz mi skrypt*); te oznaczone *bez AI* dzialaja natychmiast i nic nie kosztuja.

**Schemat** powstaje sam z tego, co Pipe wie o infrastrukturze -- nic sie nie rysuje ani nie konfiguruje:

| Poziom | Co widac |
|--------|----------|
| Serwery | ten serwer i zdalne cele (SSH, kontenery, klastry) |
| Wnetrze serwera | internet, reverse proxy, projekty compose, pojedyncze kontenery, uslugi i porty |
| Projekt | kontenery projektu, ich porty i zaleznosci (aplikacja -> baza), katalog projektu |

- Gdy agent cos robi, element, ktorego to dotyczy, jest podswietlony, a po polaczeniach plyna impulsy. Dzialanie
  czekajace na Twoja zgode ma inna, przerywana obwodke. Po zakonczeniu element blyska na zielono albo czerwono.
- **Sledze agenta** (domyslnie wlaczone) przenosi widok za agentem, takze miedzy poziomami. Dowolny wlasny ruch --
  przesuniecie, zoom, klikniecie -- wylacza sledzenie; przycisk wlacza je z powrotem.
- Klikniecie w element pokazuje jego stan, alerty oraz co agent robi na nim teraz i co robil ostatnio; w projekt
  albo serwer -- wchodzi poziom nizej.
- Pod schematem jest os czasu dzialan, z krokami bezpiecznika (kopia, sprawdzenie, weryfikacja) i workerow.

Przypisanie dzialania do elementu wynika z tresci komendy: nazwa kontenera, jednostka systemd, sciezka pliku, katalog
projektu. Gdy sie nie da (np. dowolny skrypt), dzialanie jest pokazywane na calym serwerze. Wyszukiwanie i czytanie
stron (`web_search`, `web_fetch`, `software_info`) swieci na elemencie *Internet*.

**Panel boczny** zajmuje domyslnie pol ekranu. Jego krawedz mozna przeciagnac -- wezej albo szerzej, najwyzej do polowy
ekranu; dwuklik na krawedzi przywraca domyslna szerokosc, a przeciagniecie do prawej krawedzi okna chowa panel.
Panel chowa tez przycisk w jego pasku zakladek; przycisk w gornym pasku chowa go i rozwija z powrotem. Ze schowanym
panelem rozmowa zajmuje cale okno (tresc w czytelnej kolumnie posrodku), a komenda albo powiadomienie, ktore otwiera
zakladke (np. `/alerty`), rozwija panel sam. Szerokosc i stan panelu zostaja w przegladarce. Z klawiatury: fokus na
krawedzi, strzalki zmieniaja szerokosc, Enter chowa.

**Zmiany.** Przed zatwierdzeniem zapisu pliku widac roznice w karcie potwierdzenia. Po wykonaniu roznica "przed -> po"
pojawia sie w rozmowie i w zakladce *Zmiany* -- takze gdy plik zmienila komenda (np. `sed`), bo bezpiecznik robi kopie.
Sekrety w roznicach sa redagowane. Kazdy wpis ma przycisk cofniecia. Roznic nie ma dla katalogow i danych w bazach.

**Komendy i skille.** Wpisanie `/` w polu rozmowy rozwija liste komend (`/status`, `/nowosci`, `/raport`, `/audyt`...)
i skilli z opisami -- wybor strzalkami albo kliknieciem, Tab uzupelnia komende, zeby dopisac argumenty. `/skille`
pokazuje zapisane procedury w rozmowie, z podgladem tresci i przyciskiem uruchomienia. Panel ma trzy zakladki:
*Pulpit*, *Schemat* (kropka przy nazwie, gdy agent wlasnie pracuje) i *Zmiany*; ostatnio wybrana zostaje w przegladarce.

**Provider LLM.** Przy pierwszym wejsciu strona pyta, z ktorym providerem ma pracowac Pipe: wybierasz go z listy,
wklejasz klucz API (jest sprawdzany u providera, zanim zostanie zapisany) i wybierasz model z jego aktualnej listy.
Ekran mozna pominac -- zostaje provider z instalacji. Nad polem rozmowy jest przelacznik pokazujacy, kto odpowiada;
rozwija liste providerow, do ktorych jest klucz, i przelacza bez restartu, w trakcie rozmowy. `/provider` otwiera
ekran ponownie (kolejny klucz, inny model, usuniecie klucza). Klucze leza na serwerze w `DATA_DIR/llm_keys.json`
(0600) i nie wracaja do przegladarki. Wybor dotyczy calego agenta: po przelaczeniu z nowego providera korzystaja tez
Telegram, CLI, rutyny i workery; `WORKER_MODEL` i ceny z `.env` dotycza tylko providera bazowego. Kilka kluczy mozna
tez podac od razu w `backend/.env` (`OPENAI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`...).

## Pipe w przegladarce z telefonu i komputera (`/web`)

`pipe web` dziala na Twoim komputerze przez tunel SSH. Zeby wejsc do Pipe z telefonu albo z dowolnego komputera
bez tunelu, napisz w Telegramie, CLI albo `pipe web` jedna komende:

```
/web wlacz
```

Pipe stawia przed soba serwer HTTPS (Caddy w kontenerze `pipe-web`) i odpisuje adresem, np.
`https://203-0-113-9.sslip.io` -- to darmowa nazwa, ktora wskazuje na IP Twojego serwera, wiec certyfikat Let's
Encrypt powstaje sam, bez kupowania domeny. Z wlasna domena: `/web wlacz pipe.mojafirma.pl` (rekord DNS musi juz
wskazywac na serwer). Port to 443, a gdy jest zajety (np. przez nginx) -- 8443; wlasny: `/web wlacz 9443`.

**Na mikr.us** Pipe rozpoznaje serwer (plik `/klucz_api`) i korzysta z subdomen mikr.us: strona nasluchuje na
IPv6, a mikr.us sam daje adres `https://<serwer>-7443.mikrus.cloud` z certyfikatem. Nic nie trzeba otwierac ani
zakladac, adres sie nie zmienia, ruch nie idzie przez nikogo poza mikr.us. Wymuszenie: `/web wlacz mikrus`.

**Gdy dostawca nie wpuszcza ruchu na 80/443** (inne serwery na wspoldzielonym adresie, serwer w domu za
routerem, panel chmury z zablokowanymi portami), certyfikatu nie da sie wystawic. Pipe sprawdza to sam: jesli
publiczny adres nie jest adresem serwera albo certyfikat nie powstaje, przechodzi na **tunel Cloudflare** i odpisuje
adresem w rodzaju `https://quiet-river-1a2b.trycloudflare.com`. Tunel to kontener `cloudflared`, ktory sam laczy sie
z Cloudflare -- nie trzeba otwierac zadnego portu ani zakladac konta. Cena: ruch przechodzi przez Cloudflare (to on
szyfruje polaczenie z przegladarka), szybki tunel nie ma gwarancji dzialania, powiadomienia na stronie przychodza co
kilka sekund zamiast od razu, a adres zmienia sie po restarcie serwera -- Pipe zauwaza nowy i przysyla go w
wiadomosci (urzadzenia wpuszcza sie wtedy jeszcze raz). Wymuszenie: `/web wlacz tunel` albo
`/web wlacz bezposrednio`.

Zanim Pipe poda link, sam go otwiera z internetu i sprawdza, czy odpowiada strona prosby o dostep. Gdy nie, mowi
dlaczego i pokazuje ostatnie linie z logow Caddy albo `cloudflared`.

**Jak sie wchodzi.** Strona nie ma hasla ani tajnego linku. Otwierasz adres, naciskasz *Popros o dostep* i widzisz
kod, np. `K7P-4QX`. W tym samym momencie Pipe pisze do Ciebie (Telegram, `pipe web` na innym urzadzeniu, CLI):
*"Ktos chce wejsc do Pipe przez przegladarke -- iPhone, Safari, IP ..., kod K7P-4QX"* z przyciskami *Wpusc*,
*Tylko podglad* i *Odrzuc* (albo piszesz `/web wpusc K7P-4QX`). Sprawdzasz, czy kod sie zgadza, i naciskasz *Wpusc*
-- strona sama sie otwiera. Kazde urzadzenie wpuszczasz raz: telefon osobno, komputer osobno. Potem pamieta
logowanie przez 30 dni od ostatniego uzycia.

- `/web` -- adres, lista urzadzen (co to za urzadzenie, pelny dostep czy podglad, kiedy ostatnio) i prosby czekajace
  na zgode.
- `/web odwolaj 2` albo `/web odwolaj wszystkie` -- urzadzenie traci dostep od razu, otwarta strona wraca do prosby
  o dostep.
- `/web wylacz` -- zatrzymuje Caddy albo tunel i most, zamyka porty, ktore Pipe otworzyl w ufw. Urzadzenia zostaja na wypadek
  ponownego wlaczenia.

Urzadzenie wpuszczone przyciskiem *Tylko podglad* ma role viewer: rozmawia, czyta raporty i schemat, ale niczego
nie zmienia. Wpuszczac moze tylko administrator. Strona na telefonie to ten sam interfejs co `pipe web` -- rozmowa
i pulpit przelacza sie przyciskami w gornym pasku.

Gdy na serwerze dziala ufw, Pipe otwiera w nim port strony (i 80, gdy strona nie jest na 443 -- tam Let's Encrypt
sprawdza serwer przy wystawianiu certyfikatu). Zapory u dostawcy (panel chmury) Pipe nie widzi: jesli strona nie
odpowiada, Pipe mowi, ktory port otworzyc. Dziala w trybie docker i native (potrzebny Docker -- Caddy jest
kontenerem), nie w trybie obserwacji ani w Kubernetes (tam Ingress). Szczegoly zabezpieczen: `docs/security.md`
(*Pipe w internecie*).

---

## Widoki: prosty i techniczny

Pipe ma dwa widoki, jak jedna aplikacja do dwoch rzeczy. **Prosty** jest dla osob nietechnicznych: rozmowa zwyklymi
slowami, przypomnienia, pliki, skille i dodatki do aplikacji zewnetrznych (serwery MCP: GitHub, kalendarz, poczta).
Agent sam wybiera sposob wykonania zadania, nie kaze wpisywac komend i pyta o zgode prostym pytaniem tak/nie.
**Techniczny** ma to samo i do tego serwer: schemat na zywo, dziennik zmian z cofaniem, podatnosci i latanie, kontenery,
zdalne serwery.

Przy pierwszym uruchomieniu Pipe pyta, co chcesz robic. Mozna odpowiedziec kilkoma slowami (*"przypomnienia i maile"*,
*"pilnowac serwera ze sklepem"*) albo wybrac widok. Widok jest wspolny dla calego agenta, tak jak jezyk: `/widok prosty`,
`/widok techniczny` (`/view`) w CLI, w `pipe web` (takze przycisk w naglowku) i na Telegramie (przyciski). Zmiana
w jednym kanale przelacza pozostale. Zapis w `DATA_DIR/view.json`, zmienia tylko administrator. Dopoki nikt nie
wybral, obowiazuje widok techniczny, czyli to, co Pipe pokazywal dotad.

| Gdzie | Widok prosty | Widok techniczny |
|-------|--------------|------------------|
| `pipe web` | zakladki *Pulpit* (bez wykresow i projektow) i *Dodatki*, podpowiedzi na co dzien | *Pulpit*, *Schemat*, *Zmiany*, *Dodatki* |
| CLI | `/pomoc` i podpowiedzi `/` bez komend serwera | wszystkie komendy |
| Telegram | menu `/`, `/pomoc` i klawiatura: stan, nowosci, skille, pomoc | menu i klawiatura jak dotad |
| Agent | blok *WIDOK PROSTY* w prompcie: zwykle slowa, bez zargonu | bez zmian |

Komendy serwera (`/zalataj`, `/mapa`, `/audyt` i inne) dzialaja w obu widokach po wpisaniu, widok prosty tylko ich
nie podsuwa. Zakladka *Dodatki* w `pipe web` zbiera skille (uruchomienie, podglad), podlaczone serwery MCP i dodawanie
nowych z GitHuba (`/pobierz`, `/mcp dodaj`).

**Pierwsze uruchomienie bez serwera.** `pipe web` bez `--host` i bez zapisanego polaczenia otwiera kreator: najpierw
widok, potem miejsce. *Na tym komputerze* uruchamia backend w Dockerze z tego repozytorium (na Linuksie Pipe zarzadza
tym komputerem, na Windows i macOS pracuje we wlasnym kontenerze Docker Desktop, `backend/docker-compose.desktop.yml`).
*Na moim serwerze* laczy sie przez SSH z adresem i haslem albo kluczem, klonuje repozytorium do `/opt/pipe`
i uruchamia `install-server.sh` bez pytan (bez modelu; klucz API dodajesz potem na stronie). Haslo zostaje w procesie
`pipe web`: nie trafia na dysk, do backendu ani do modelu. Po hasle Pipe dopisuje na serwerze wlasny klucz
(`~/.pipe/keys`) i kolejne polaczenia ida juz nim. Polaczenie zapisuje sie w `~/.pipe/app.json`, wiec nastepne `pipe web`
i zwykle `pipe` lacza sie same. Kreator wraca z `pipe web --setup` albo po kliknieciu adresu serwera w naglowku.

---

## Jezyk: polski albo angielski

`PIPE_LANG=pl` (domyslnie) albo `PIPE_LANG=en` przelacza caly produkt, nie tylko jezyk odpowiedzi modelu.
Jezyk mozna tez zmienic w trakcie pracy, bez restartu: `/jezyk en` / `/jezyk pl` (`/language`) w CLI, w `pipe web`
(komenda albo przycisk PL/EN w naglowku) i na Telegramie. Wybor jest wspolny dla calego agenta -- pozostale kanaly
przelaczaja sie same -- zapisuje sie w `DATA_DIR/language.json` i obowiazuje do recznej zmiany `PIPE_LANG` w `.env`.
Zmienia tylko administrator.

| Warstwa | Co sie zmienia |
|---------|----------------|
| Agent | system prompt, opisy 24 narzedzi, prompty workerow, rutyn, skanu serwera i VIBE |
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

**Polaczenie z laptopa** -- most stdio w CLI, przez ten sam tunel SSH i tokeny. Gotowa konfiguracja dla Claude Code,
Claude Desktop, Cursora, Codexa, Hermes Agent i OpenClaw: `pipe connect <aplikacja> --host root@serwer`
(`docs/cli.md`). Recznie:

```json
{"mcpServers": {"pipe": {"command": "pipe", "args": ["--mcp", "--host", "root@serwer"],
                         "env": {"AGENT_TOKEN": "<token>"}}}}
```

Osobny token dla agenta: `python3 -m backend.tokens add claude-code --role admin` (albo `--role viewer`:
agent tylko czyta, nie moze prosic o zgody). **Na serwerze** -- endpoint Streamable HTTP: `MCP_PORT=7381`,
`http://127.0.0.1:7381/mcp`, token w `Authorization: Bearer`.

### Pipe jako klient MCP

Pipe korzysta tez z cudzych serwerow MCP -- np. poczty, kalendarza, GitHuba albo Grafany -- jak z wlasnych narzedzi.

**Okno `/mcp`.** `/mcp` bez argumentow otwiera okno (jak `/mcp` w Claude Code): w CLI ramka wybierana strzalkami,
w `pipe web` osobny panel, na Telegramie lista z przyciskami. Widac w nim podlaczone serwery ze stanem, a po wybraniu
-- co dokladnie dziala, jakie ma narzedzia (ktore pytaja o zgode) i przyciski *Polacz ponownie*, *Wylacz*, *Usun*.

**Gotowe dodatki** (`backend/addons/`, nic nie jest wlaczone z gory):

| Dodatek | Co daje | Czego potrzebuje |
|---|---|---|
| `outlook` | poczta, kalendarz i kontakty konta Microsoft (outlook.com, hotmail) | nic -- po dodaniu napisz *"zaloguj mnie do Outlooka"*: link microsoft.com/devicelogin i kod, logowanie zostaje po restarcie |
| `microsoft365` | Outlook i Teams konta firmowego albo szkolnego | to samo logowanie kodem; niektore firmy wymagaja zgody administratora |
| `google` | Gmail (czytanie, szkice), Kalendarz (odczyt), Dysk, Dokumenty, Arkusze | wlasny klient OAuth w Google Cloud i refresh token -- okno prowadzi krok po kroku |
| `github` | repozytoria, issues, pull requesty, Actions | token GitHuba (fine-grained) |

Klucze wpisuje sie w oknie `/mcp` w `pipe web` albo w CLI (ukryte pole). Leza w `backend/data/mcp_secrets.json` (0600)
i nie trafiaja do modelu ani do przegladarki; Telegram kluczy nie przyjmuje. W rozmowie tez dziala: *"podlacz mi
Outlooka"* -- agent zna katalog (`mcp_manage catalog`) i dodaje dodatek po jednym TAK. Dodatki z logowaniem
(`state: true`) dostaja wlasny wolumen Dockera na tokeny; usuniecie dodatku usuwa go razem z zapisanym logowaniem.
Nowy dodatek to jeden plik JSON -- opis w `backend/addons/README.md`.

Wlasny serwer, spoza katalogu:

```
/mcp dodaj github npx -y @modelcontextprotocol/server-github GITHUB_PERSONAL_ACCESS_TOKEN=${MCP_GITHUB_TOKEN}
/mcp dodaj fetch uvx mcp-server-fetch
/mcp dodaj docs https://mcp.example.com/mcp TOKEN=${MCP_DOCS}
/mcp                      # lista i stan;  /mcp usun github;  /mcp odswiez
```

Albo w rozmowie: *"podlacz serwer MCP GitHuba"* -- wtedy jedno TAK z podgladem tego, co Pipe uruchomi.

| | |
|---|---|
| Gdzie dziala | `npx`, `uvx` i obraz Dockera (takze `docker run ...` z `.mcp.json` innych aplikacji) -- zawsze w osobnym kontenerze: bez plikow serwera, bez `docker.sock`, bez uprawnien (`--cap-drop ALL`, uzytkownik `nobody`), z limitem pamieci i procesow. Serwer widzi tylko siec. Serwery HTTP -- pod podanym adresem |
| Sekrety | tylko jako `${MCP_NAZWA}` -- wpisane w oknie `/mcp` albo w `backend/.env` (np. `MCP_GITHUB_TOKEN=...`); nie wpisuj tokenu w czat; trafia do kontenera jako zmienna, nigdy w argumentach |
| Zgody | narzedzie oznaczone przez serwer jako odczyt -- bez pytania; kazde inne -- TAK z widocznymi argumentami |
| Tokeny modelu | narzedzia serwera to grupa `mcp:<nazwa>` ladowana na zadanie: zwykla rozmowa ich nie wysyla, wiadomosc z nazwa serwera (*"na githubie..."*) je wlacza |
| Wynik | tresc z zewnatrz: w tej turze YOLO jest wylaczone; argument z sekretem nie wychodzi do serwera |
| Tryb obserwacji, viewer | tylko narzedzia-odczyty; nowego serwera nie da sie dodac |

Konfiguracja: `backend/data/mcp_servers.json` (0600). Start serwera stdio trwa od kilkunastu sekund do minuty (`npx` /
`uvx` instaluja pakiet w kontenerze); Pipe laczy sie w tle po restarcie.

Obslugiwane wersje protokolu (w obie strony): 2026-07-28 (`server/discover`, wersja w `_meta`, naglowki
`Mcp-Method`/`Mcp-Name`) i starsze z `initialize` (2025-11-25, 2025-06-18, 2025-03-26).

---

## Internet bez kluczy API

Agent sam siega do sieci, gdy odpowiedz zalezy od wiedzy spoza serwera -- nie trzeba go o to prosic ani
podawac zadnego klucza:

- `web_search` -- DuckDuckGo, a gdy nie odpowiada: Stack Exchange (Server Fault, Stack Overflow), potem Wikipedia.
- `web_fetch` -- czyta strone; z pytaniem strone czyta osobny model bez narzedzi i oddaje agentowi tylko odpowiedz.
- `software_info eol` -- czy wersja jest wspierana i do kiedy (endoflife.date); `software_info vulns` -- znane
  podatnosci pakietu w zainstalowanej wersji (OSV.dev), z wersja, ktora je naprawia.

Przyklady: *"nginx sypie upstream prematurely closed -- co to?"*, *"czy nasz PostgreSQL jest jeszcze wspierany?"*,
*"czy nginx na tym serwerze ma znane dziury?"*. Agent podaje zrodla. Zabezpieczenia (sekrety, adresy spoza wynikow,
siec prywatna, wstrzymanie YOLO) opisuje `docs/security.md`. `WEB_SEARCH=off` wylacza internet.

## Aktualizacja samego Pipe

Wpisz `/aktualizuj` (po angielsku `/update`) albo napisz agentowi "zaktualizuj sie"; `/aktualizuj sprawdz` tylko
pokazuje wersje i to, czy jest nowsza. Agent sprawdza zdalne repozytorium, a po Twoim
potwierdzeniu uruchamia osobny kontener pomocniczy, ktory w katalogu instalacji wykonuje `git pull --ff-only`
i podmienia uslugi Pipe na podpisany obraz wydania z GHCR (a gdy go nie ma, buduje je: `docker compose up -d --build`). Osobny kontener jest potrzebny, bo agent nie moze
wymienic kontenera, w ktorym sam dziala -- proces zginalby w polowie.

- Backend jest niedostepny przez minute lub kilka. Rozmowa wraca po restarcie (sesje leza w `DATA_DIR/sessions`), ale
  operacja czekajaca wtedy na TAK przepada i agent zaproponuje ja ponownie.
- Po restarcie przychodzi wiadomosc z wynikiem do tego, kto zlecil aktualizacje. Jesli pobranie albo budowanie sie
  nie uda, stara wersja dziala dalej, a wiadomosc zawiera koniec logu.
- Przebudowywane sa tylko uslugi, ktore juz dzialaly (bot Telegrama nie wystartuje, jesli go nie uzywasz).
- Lokalne zmiany w plikach repozytorium moga zablokowac `git pull` -- agent ostrzega o nich przy sprawdzaniu.
- Tylko tryb Docker. W trybie native: `git pull` i `sudo systemctl restart pipe`; w Kubernetesie: nowy obraz.
