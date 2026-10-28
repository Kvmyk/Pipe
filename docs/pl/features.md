# Workery, cele, rutyny, czuwanie, diagramy i historia serwera -- Pipe

Pipe v0.49.2

Ten dokument opisuje funkcje, które odróżniają Pipe od agentów ogólnego przeznaczenia:
agent, który sam pilnuje serwera, widzi jego architekturę i zarządza wieloma maszynami naraz.

---

## Pipe pisze pierwszy

Nie musisz pytać, co się dzieje. Pipe pisze sam -- na Telegram, w `pipe web` i w CLI -- gdy coś wymaga uwagi,
i od razu daje przycisk, który sprawę załatwia. Wszystko, co wyśle, trafia też do **skrzynki**
(`backend/data/feed.json`), więc nic nie ginie, gdy nikt nie był podłączony.

| Wiadomość | Kiedy | Przyciski |
|-----------|-------|-----------|
| Alert | problem już jest: dysk, pamięć, kontener, strona, certyfikat, podatności | *Zwolnij miejsce*, *Załataj*, *Zbadaj* |
| Ustalenia | krytyczny problem: Pipe sam sprawdza przyczynę (worker, tylko odczyty) i pisze, co ustalił | *Napraw*: rutynowe poprawki od razu, ważne czekają na Twoje TAK |
| Podpowiedź | problem dopiero będzie albo coś można poprawić (tabela niżej) | akcja, *Później*, *Mniej takich* |
| Raport poranny | o `DIGEST_TIME` | -- |
| Rozwiązane | problem minął | -- |

**Podpowiedzi** liczą się bez modelu językowego, z danych, które Pipe i tak zbiera (co `INSIGHTS_INTERVAL`, 1 h):

| Podpowiedź | Skąd |
|------------|------|
| *Dysk / zapełni się za ok. 4 dni* | tempo przyrostu z historii pomiarów (regresja z ostatnich 48 h) |
| *Mogę zwolnić ok. 6 GB* | obrazy Dockera bez tagu, cache budowania, pobrane paczki apt |
| *Kontener X zrestartował się 4 razy w ciągu doby* | licznik restartów (pętle restartów łapie alert) |
| *Serwer od 5 dni czeka na restart* | `/run/reboot-required` |
| *Pamięć RAM stale rosnie* | zajętość RAM rosnąca przez dobę (możliwy wyciek) |

**Samodzielne badanie.** Gdy pojawia się krytyczny problem (kontener przestał działać, strona nie odpowiada, dysk
pełny), Pipe nie czeka na *Zbadaj*: worker sprawdza logi, zasoby i zmiany z ostatniej doby (tylko odczyty), a ustalenia
przychodzą zaraz po alercie, z przyciskiem *Napraw*. `AUTO_INVESTIGATE=critical` (domyślnie), `all` albo `off`;
najwyżej `AUTO_INVESTIGATE_DAILY` (4) badań dziennie, ten sam problem najwyżej raz na 6 h.

**Kiedy powiadamiać.** Poziom (`NOTIFY_LEVEL`): `all` -- wszystko; `important` (domyślnie) -- problemy, ostrzeżenia,
raport i ważne podpowiedzi, informacyjne czekają w skrzynce; `critical` -- tylko to, co nie może czekać. Cisza nocna
(`QUIET_HOURS=22:00-07:00`): w nocy przychodzą tylko sprawy krytyczne, reszta rano razem z raportem. Najwyżej
`INSIGHTS_PER_DAY` (3) podpowiedzi dziennie z powiadomieniem. *Mniej takich* wycisza dany rodzaj podpowiedzi (zostaje w
skrzynce bez powiadomienia), *Później* odkłada ją o dobę. Te same ustawienia zmienisz w trakcie pracy:
`/powiadomienia` (Telegram -- przyciskami, CLI -- `/powiadomienia wazne cisza 22-7 dziennie 3`, `pipe web` -- dzwonek na
pulpicie).

- `/nowosci` (`/news`): skrzynka z akcjami (w CLI wpisujesz np. `1a`). Krótki stan bez modelu: `/status`.
- Przyciski buduje backend -- klient odsyła tylko numer akcji (`feed_act`). *Napraw* i *Załataj* są dla administratora.
- Klienci powiadamiają tylko o wiadomościach z `push` (poziom, cisza nocna, limit); CLI pokazuje je nad promptem,
  `pipe web` jako okienko, Telegram jako wiadomość.

---

## Warsztat: projekty, które Pipe stawia sam

Napisz *"postaw mi tę aplikację: https://github.com/acme/notatnik"* albo *"napisz skrypt, który policzy zamówienia z
logów"*, a Pipe poprowadzi to do końca sam. Projekty leżą w jednym katalogu hosta -- **warsztacie**
(`WORKSPACE_DIR`, domyślnie `/root/pipe-workspace`), każdy w swoim podkatalogu i w `backend/data/projects.json`.

| Krok | Co robi Pipe | Pyta? |
|------|--------------|-------|
| `clone` | `git clone --depth 1` z adresu https albo `git@` (klucz SSH serwera); bez hooków, submodułów, LFS; za duże repozytorium (`PROJECT_MAX_MB`) usuwa | nie |
| `inspect` | co to jest i jak to uruchomić -- bez modelu: compose, Dockerfile, Node, Python (Django, FastAPI, Flask, Streamlit), Go, PHP, strona statyczna; porty, potrzebne zmienne, README i AGENTS.md | nie |
| zapis plików | `write_file` w katalogu warsztatu (np. Dockerfile, skrypt) | nie |
| `exec` | komenda w jednorazowym kontenerze: tylko katalog projektu jako `/work`, bez `docker.sock` i uprawnień (`--cap-drop ALL`, `no-new-privileges`), limit pamięci, procesów i czasu | nie* |
| `run` | usługa na stałe: kontener `pipe-<nazwa>` (albo projekt compose `pipe-<nazwa>`), port tylko na `127.0.0.1` | nie -- plan i postęp widać w rozmowie |
| `logs`, `stop`, `start`, `pull` | logi, zatrzymanie, wznowienie, `git pull --ff-only` | nie |
| `remove` | katalog, kontenery i zbudowany obraz | **tak** |

\* Po przeczytaniu treści z zewnątrz (README sklonowanego repozytorium, strona z internetu) Pipe pyta o zgodę na
`exec` do Twojej następnej wiadomości -- jak YOLO: tekst z cudzego repozytorium nie może sam uruchomić kodu.

- Kod z repozytorium nigdy nie działa w procesie ani kontenerze Pipe (tam jest `docker.sock`). Instaluje się i
  uruchamia w kontenerach -- na serwerze nie przybywa Node.js, Pythona ani bibliotek.
- `run` jest w dzienniku zmian: `/cofnij` zatrzymuje i usuwa wdrożenie. Ponowne `run` zostaje na tym samym porcie.
- Projekt compose z ryzykownymi ustawieniami (`privileged`, `docker.sock`, sieć hosta, montowanie `/etc`) albo
  portami otwartymi na świat -- potwierdzenie mówi to wprost.
- Gdy `inspect` nie wie, jak uruchomić projekt, agent pisze Dockerfile w jego katalogu i uruchamia `run strategy=dockerfile`.
- Adres aplikacji to `http://127.0.0.1:<port>` na serwerze (z laptopa: `ssh -L <port>:127.0.0.1:<port> <serwer>`);
  wystawienie pod domeną przez reverse proxy to osobna prośba (zwykłe potwierdzenie).
- `/projekty` (`/projects`) w CLI i na Telegramie, karty projektów z logami i zatrzymaniem na pulpicie `pipe web`.
  `WORKSPACE_AUTONOMY=0` -- zgoda na każdą operację. Tryb obserwacji i rola viewer -- tylko lista i logi.

---

## Oszczędność tokenów

Czuwanie, podpowiedzi, raporty, `/status`, `/zmiany`, `/audyt`, `/sprzataj` i pulpit działają bez modelu
językowego -- zero tokenów. Gdy rozmowa już idzie do modelu, Pipe wysyła mniej:

- **Narzędzia na żądanie** (`TOOLS_MODE=lazy`). Opisy 28 narzędzi to ~6 tys. tokenów przy każdym kroku agenta.
  Rozmowa zaczyna od podstawowych (~2,1 tys.), a reszta włącza się sama, gdy wiadomość jej potrzebuje
  (*"przypomnij jutro"* -> przypomnienia, *"czy serwer jest bezpieczny"* -> audyt) albo gdy model poprosi
  (`load_tools`). Włączona grupa zostaje do końca rozmowy, więc cache promptu u providera działa dalej.
  `TOOLS_MODE=all` -- wszystkie zawsze.
- **Skracanie starych wyników** (`CONTEXT_KEEP_TURNS`, 2). Wyniki narzędzi starsze niż dwie tury idą do modelu jako
  początek z adnotacją (*"jeśli potrzebujesz, uruchom jeszcze raz"*). Badania JetBrains (*The Complexity Trap*, NeurIPS
  2025) pokazały, że taki zabieg obniża koszt agenta o ponad połowę bez spadku skuteczności. Historia w sesji zostaje pełna.
- **Krótszy prompt**: bez powtórzonej listy narzędzi i bez obowiązkowego prefiksu z katalogiem w każdej odpowiedzi.
- `/koszt` i pulpit pokazują, ile tokenów Pipe **nie wysłał** (narzędzia na żądanie, skrócone wyniki) i ile wejścia
  przyszło z cache providera.

---

## Diagramy

Agent rysuje diagramy w składni **Mermaid** i wysyła je jako obraz:

- **Telegram** -- zdjęcie (duże diagramy jako plik w pełnej rozdzielczości)
- **CLI** -- PNG w `~/.pipe/diagrams/` + podgląd ASCII w terminalu, jeśli się mieści
  (`pipe --open` otwiera PNG automatycznie; `/mermaid` pokazuje kod ostatniego diagramu)

Dwa tryby narzędzia `diagram`:

| Tryb | Kiedy | Skąd dane |
|------|-------|-----------|
| `infra` | *"pokaż architekturę"*, *"co tu jest postawione"*, `/mapa` | automatyczne odkrycie (poniżej) |
| `mermaid` | przepływy, procedury, zależności, *"narysuj jak działa deploy"* | kod napisany przez agenta |

**Mapa infrastruktury** (`/mapa` działa bez LLM -- szybko i za darmo) składa:

- kontenery z `docker inspect`: obraz, stan, healthcheck, opublikowane porty, projekt compose
  (kontenery projektu są w ramce, aplikacja -> baza połączone linią przerywaną)
- trasy reverse proxy: `server_name` / `proxy_pass` / `upstream` z nginx (`sites-enabled`, `conf.d`),
  bloki `reverse_proxy` z Caddyfile, etykiety Traefika (`Host(...)`) i `VIRTUAL_HOST`
- porty hosta z `/proc/1/net` -- porty wystawione na świat są połączone z *Internet*
- włączone usługi systemd (bez systemowych)
- aplikacje z klastra Kubernetes (ingress -> service -> deployment), gdy kubectl ma dostęp
- zdalne cele Pipe

Kontener wyłączony albo `unhealthy` jest czerwony. Etykiety są escapowane -- nazwa kontenera
czy domena nie złamie składni diagramu.

Renderowanie: [mermaidx](https://github.com/MohammadRaziei/mermaidx) -- prawdziwa biblioteka Mermaid
w osadzonym QuickJS + resvg. Bez przeglądarki, bez Node.js i bez wysyłania kodu diagramu do
zewnętrznej usługi (w przeciwieństwie do mermaid.ink/kroki). Błąd składni wraca do modelu, który
poprawia kod.

---

## DIRECTORY -- mapa katalogów

Uzupełnienie SERVER.md: zamiast prozy -- lista konkretnych miejsc z jednozdaniowym opisem.

```
- /srv/shop [repo] sklep (Next.js), deploy przez compose (remote https://github.com/acme/shop.git, galaz main)
- /srv/shop/docker-compose.yml [compose] projekt docker compose 'shop'
- /var/backups/pg [backup] nocne dumpy Postgresa, rotacja 7 dni
```

- `directory operation=scan` odkrywa repozytoria git (bez uruchamiania gita: `.git/config`,
  `.git/HEAD`; tokeny z adresow remote są usuwane) pod `/root`, `/home`, `/srv`, `/opt`, `/var/www`,
  `/data` oraz katalogi projektów compose z etykiet kontenerów. Skan nie nadpisuje opisów agenta.
- Agent dopisuje miejsca sam, gdy na nie trafi. Gdy mówisz o projekcie, najpierw sprawdza DIRECTORY.
- `/katalogi` w obu klientach. Plik: `backend/data/directory.json`.

---

## VIBE -- styl rozmowy

Agent z czasem dopasowuje się do tego, jak piszesz i czego oczekujesz.

- Co `VIBE_EVERY` (domyślnie 6) Twoich wiadomości agent w tle (bez spowalniania odpowiedzi) wysyła do LLM
  obecną notatkę i Twoje ostatnie wiadomości, i zapisuje zaktualizowana notatkę: ton, długość odpowiedzi,
  poziom techniczny, nawyki, czego unikać.
- Gdy powiesz wprost (*"odpowiadaj krócej"*), agent zapisze to od razu (narzędzie `vibe`).
- Osobna notatka na użytkownika: `vibe/telegram-123.md`, `vibe/cli-kuba.md`.
- Do nauki trafiają tylko Twoje wiadomości -- bez promptów składanych przez Pipe (skan, `/status`, skille).
- VIBE to wskazówki stylu: nie zmienia zasad bezpieczeństwa, potwierdzeń ani formatu interfejsu.
- `/vibe` pokazuje notatkę, `/vibe reset` ją czyści. `WORKER_MODEL` ustawia tańszy model też dla VIBE.

---

## Zdalne cele

> Eksperymentalne: działa i ma testy, ale ma za sobą mniej użycia na prawdziwych serwerach niż reszta Pipe.
> Jeśli coś nie gra, zgłoś to w issue.

Cel to miejsce, na którym Pipe wykonuje komendy **bez instalowania czegokolwiek po drugiej stronie**:

| Rodzaj | Jak Pipe wykonuje komendę | Pola |
|--------|---------------------------|------|
| `ssh` | `ssh -o BatchMode=yes ... host -- 'komenda'` | `host`, `user`, `port`, `identity_file` |
| `docker` | `docker exec kontener sh -c 'komenda'` | `container` |
| `kubernetes` (klaster) | `kubectl --context C --namespace N ...` | `context`, `namespace` |
| `kubernetes` (pod) | `kubectl exec pod -- sh -c 'komenda'` | `pod`, `pod_container` |
| `local` | ten host (zawsze dostępny) | -- |

- Dodanie celu wymaga potwierdzenia. Pola są walidowane, komenda idzie jako jeden argument
  (`shlex.quote`) -- nazwa ani parametry nie wstrzykną poleceń.
- Klasyfikator ocenia komendę **docelową**, a potwierdzenie pokazuje pełną komendę z opakowaniem.
- SSH używa kluczy z `/root/.ssh` (w Dockerze tylko do odczytu); `known_hosts` celów jest w `backend/data`.
  Uwierzytelnianie hasłem nie jest obsługiwane (BatchMode) -- dodaj klucz publiczny Pipe na celu.
- Klaster: w trybie Docker odkomentuj montowanie `/root/.kube` w `docker-compose.yml`; w trybie
  Kubernetes Pipe używa własnego ServiceAccount.
- `/cele` w obu klientach. Plik: `backend/data/targets.json`.

Przykład: *"dodaj serwer 10.0.0.5 jako db-backup, ssh, użytkownik root"* -> TAK -> *"sprawdź łączność"*.

---

## Workery

> Eksperymentalne: działa i ma testy, ale ma za sobą mniej użycia na prawdziwych serwerach niż reszta Pipe.
> Jeśli coś nie gra, zgłoś to w issue.

Worker to pod-agent, z którym rozmawia agent, a nie Ty. Narzędzie `delegate` wysyła kilka naraz:

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

- Każdy worker: jeden cel, jedno zadanie, własna historia rozmowy z LLM, jedno narzędzie `run`.
- **Tylko odczyty.** Komenda zmieniająca stan nie jest wykonywana -- trafia do raportu jako propozycja.
  Agent wykonuje ją przez `remote_exec`, czyli za Twoja zgoda. Dzięki temu workery mogą działać
  równolegle i w tle bez nikogo przy klawiaturze.
- Raport: USTALENIA / PROBLEMY / PROPOZYCJE. Postęp widać na żywo (Telegram: jedna aktualizowana wiadomość).
- Ta sama nazwa workera w kolejnym `delegate` kontynuuje jego wątek.
- Limity: `MAX_WORKERS` (6), `WORKER_TIMEOUT` (240 s), `WORKER_MAX_ITERATIONS` (8).
  `WORKER_MODEL` -- tańszy model dla workerów.

---

## Rutyny

Zadania, które agent wykonuje sam według harmonogramu i raportuje:

*"codziennie o 7 sprawdź backupy w /var/backups i ważność certyfikatów, pisz tylko jak coś jest nie tak"*

- Harmonogram cron (5 pól, czas serwera) albo `@hourly` / `@daily` (7:00) / `@weekly` / `@monthly`.
- Wykonuje je worker na wskazanym celu -- tylko odczyty.
- Raport zaczyna się od `STATUS: OK` albo `STATUS: PROBLEM`. `notify=problems` wysyła tylko problemy.
- Raporty przychodzą na Telegram (subskrypcja zdarzeń). `/rutyny` pokazuje listę i ostatni status.
- Dodanie rutyny wymaga potwierdzenia. *"uruchom rutynę poranny-przegląd teraz"* -- `routine_manage run`.

---

## Przypomnienia

Rutyna jest cykliczna; przypomnienie odpala się **raz**, o podanym czasie, i znika:

*"napisz do mnie za 10 sekund"* · *"przypomnij jutro o 9 o odnowieniu domeny"* · *"za godzinę sprawdź, czy backup się skończył"*

- Czas względny (`10s`, `15m`, `2h`, `1d`, `1h30m`) albo bezwzględny (`HH:MM`, `RRRR-MM-DD HH:MM`, czas serwera).
- **Wiadomość** (`kind=message`) -- sama treść, bez LLM i bez potwierdzenia; ustawienie widać jako notkę `[PAMIEC]`.
- **Zadanie** (`kind=task`) -- o czasie worker wykonuje je na wskazanym celu (tylko odczyty) i przysyla raport.
  Worker tylko czyta, więc Pipe zakłada je bez pytania (przy `AUTONOMY=0` po potwierdzeniu).
- Dostarczenie: Telegram, kanałem zdarzeń. Przypomnienie z Telegrama wraca do osoby, która je ustawiła; z CLI --
  do administratorów. Gdy bot nie jest podłączony, przypomnienie czeka i dociera po połączeniu.
- CLI nie subskrybuje zdarzeń, ale czekając na wpis co 10 s odbiera przypomnienia (także wynik `/aktualizuj`) i pokazuje
  je nad promptem. Przypomnienie ustawione z CLI idzie do subskrybentów (Telegram, `pipe web`) i dodatkowo czeka na odbiór w CLI.
- Rejestr `backend/data/reminders.json` przetrwa restart; limit 50 przypomnień, najdalej rok naprzód.
- `/przypomnienia` -- lista z czasem serwera; `/przypomnienia anuluj <id>` -- anulowanie (bez LLM).

Agent nie potrafi czekać w trakcie odpowiedzi. Bez tego narzędzia obietnica *"odezwę się później"* byłaby pusta --
prompt zabrania jej, jeśli nic nie zostało zaplanowane.

---

## Czuwanie

Deterministyczne sprawdzenia co `WATCH_INTERVAL` (120 s), bez LLM i bez kosztów:

| Sprawdzenie | Prog | Poziom |
|-------------|------|--------|
| Zajętość każdego dysku hosta | `WATCH_DISK_PCT` (90%), >= 97% krytyczny | ostrzeżenie / krytyczny |
| RAM | `WATCH_MEM_PCT` (92%) | ostrzeżenie |
| Load average 5 min | rdzenie x `WATCH_LOAD_FACTOR` (2) | ostrzeżenie |
| Kontener restartuje się w pętli | -- | krytyczny |
| Kontener `unhealthy` | -- | ostrzeżenie |
| Kontener, który działał, przestał działać | -- | krytyczny (jednorazowo) |
| **Nowy publiczny port na hoście** | względem poprzedniego sprawdzenia | ostrzeżenie (jednorazowo) |

- Alert jest wysyłany raz, a gdy problem minie -- przychodzi *ROZWIAZANE*.
- Alert ma przyciski (**Zbadaj**, przy dysku **Zwolnij miejsce**, przy podatnościach **Załataj**); krytyczny Pipe
  bada sam i dosyła ustalenia (sekcja *Pipe pisze pierwszy*).
- Aktywne alerty są w prompcie agenta -- pytany o stan serwera, uwzględni je. `/alerty` w obu klientach.
- Stan (poprzednie porty i kontenery) w `backend/data/watch_state.json`. `WATCH_ENABLED=0` wyłącza.

---

## Co się zmieniło -- wehikuł czasu serwera

Najczęstsze pytanie przy awarii: *"co się zmieniło, że przestało działać?"*. Pipe zna odpowiedź, bo co
`SNAPSHOT_INTERVAL` (1 h) robi migawkę hosta -- same odczyty plików i `docker inspect`, bez LLM:

| Sekcja | Co widać |
|--------|----------|
| System | jądro, restart serwera (nowy rozruch) |
| Pakiety | instalacje, aktualizacje, usunięcia (dpkg, apk) |
| Kontenery | nowe i usunięte, zmiana obrazu **albo nowej wersji obrazu pod tym samym tagiem**, zatrzymanie |
| Porty | nowe i zamknięte porty TCP hosta |
| Usługi | wlaczone/wylaczone usługi systemd, zmienione pliki jednostek |
| Cron | dodane i usunięte linie (sekrety zredagowane) |
| Konta | nowe konta z powłoką albo uid 0 |
| Klucze SSH | nowe klucze w `authorized_keys` (odcisk + komentarz; sam klucz nie jest zapisywany) |
| Konfiguracja | sshd, sudoers, nginx, Caddy, HAProxy, fstab, hosts, `daemon.json`, pliki compose projektów |

- Migawka jest zapisywana tylko, gdy coś się zmieniło. Ostatnie 48 h -- wszystkie, starsze -- jedna na dzień,
  maksymalnie `SNAPSHOT_KEEP_DAYS` (30) dni. Pliki: `backend/data/snapshots/`.
- `/zmiany [24h|3d]` pokazuje zmiany pogrupowane w przedziały *"między 03:00 a 04:00"*.
- Agent sam zaczyna od historii zmian, gdy słyszy *"przestało działać"*, a przy **Zbadaj** (alert) backend
  dołącza zmiany z ostatniej doby do polecenia.
- Zmiany kont, kluczy SSH, sudoers i sshd są oznaczone **[BEZPIECZENSTWO]**.

---

## Wykresy

Czuwanie przy każdym sprawdzeniu (co 2 min) zapisuje próbkę: load, RAM, swap, zajętość dysków. Historia
(`METRICS_KEEP_DAYS`, 8 dni) daje wykresy bez Prometheusa:

- `/wykres` -- load z ostatniej doby; `/wykres ram 7d`, `/wykres dysk 3d`
- *"czy RAM rosnie od tygodnia?"* -- agent rysuje wykres (narzędzie `server_history`, `operation=chart`)
  i dostaje liczby (min, max, średnia, przekroczenia progu), bo sam obrazu nie widzi
- Czerwona linia na wykresie to prog alertu czuwania.

Wykres to Mermaid `xychart-beta` renderowany tym samym silnikiem co diagramy (offline).

---

## Monitoring bez konfiguracji

Pipe wie, co jest na serwerze, więc sam wie, co sprawdzać -- co `CHECKS_INTERVAL` (1 h):

| Sprawdzenie | Skąd Pipe wie, co sprawdzić | Alert |
|-------------|-----------------------------|-------|
| Ważność certyfikatu TLS | domeny z nginx, Caddy, etykiet Traefika / `VIRTUAL_HOST` | `WATCH_CERT_DAYS` (14) dni przed wygaśnięciem; krytyczny 3 dni przed albo gdy certyfikat jest nieprawidłowy |
| Odpowiedź HTTPS | te same domeny | 5xx albo brak odpowiedzi **dwa razy z rzędu** (chwilowe 502 przy deployu nie alarmuje) |
| DNS | te same domeny | domena się nie rozwiązuje; w `/zdrowie` -- czy wskazuje na ten serwer |
| Świeżość backupów | wpisy `[backup]` w DIRECTORY | najnowszy plik starszy niż `WATCH_BACKUP_HOURS` (26 h), pusty albo brakujący katalog |

- `/zdrowie` -- wszystko naraz, z liczbą dni do wygaśnięcia każdego certyfikatu.
- Katalog backupów dopiszesz zdaniem: *"backupy bazy leżą w /var/backups/pg"* (agent doda wpis `[backup]`).
- `WATCH_SITES=0` wyłącza sprawdzenia sieciowe, `WATCH_IGNORE=a.example.com,/var/backups/stare` pomija wybrane.

---

## Poranny raport

Codziennie o `DIGEST_TIME` (7:00, czas serwera; `off` wyłącza) na Telegram przychodzi raport -- **bez LLM**:

- **Stan** -- uptime, load względem rdzeni, RAM, dyski
- **Alerty** -- aktywne i zdarzenia z ostatniej doby
- **Zmiany od wczoraj** -- najpierw zmiany bezpieczeństwa, potem kontenery, porty, usługi; pakiety zbiorczo
- **Zdrowie** -- najbliżej wygasające certyfikaty, strony, backupy
- **Aktualizacje** -- liczba pakietów do aktualizacji i *wymagany restart* (Ubuntu/Debian)
- **Podatności** -- ile pakietów serwera ma poprawkę (w tym pilnych i aktywnie wykorzystywanych), trzy najgroźniejsze
  z opisem, czy serwer czeka na restart, zdalne serwery, obrazy i aplikacje z poprawkami; na końcu `/podatnosci`
  i `/zalataj`
- **Rutyny** -- ostatni status każdej rutyny
- **LLM** -- zużycie tokenów z poprzedniego dnia

Do raportu dołączony jest wykres obciążenia z ostatniej doby. `/raport` -- raport na żądanie.

---

## Koszty LLM

Każde zapytanie do providera (rozmowa, workery, rutyny, VIBE) jest liczone w `backend/data/usage.json`.

- `/koszt` -- tokeny dziś (z podziałem na użytkownika, workery, rutyny, VIBE i model), ostatnie dni, miesiąc, oraz ile
  tokenów Pipe nie wysłał i ile wejścia przyszło z cache (sekcja *Oszczędność tokenów*).
- Koszt w USD pojawi się po podaniu cen modelu: `LLM_PRICE_IN` i `LLM_PRICE_OUT` (USD za milion tokenów),
  osobno `WORKER_PRICE_IN/OUT` dla `WORKER_MODEL`.
- `DAILY_TOKEN_LIMIT` / `DAILY_COST_LIMIT` -- po przekroczeniu agent odpowiada komunikatem o limicie do północy
  i nie wysyła zapytań do providera. Czuwanie, raport i komendy bez LLM (`/mapa`, `/zmiany`, `/wykres`) działają dalej.

---

## Zmiany z bezpiecznikiem i `/cofnij`

Rutynowe zmiany Pipe robi sam: restart i przeładowanie usług, `docker compose up -d`, edycje konfiguracji, pakiety
z repozytorium systemu, `git pull`. Przechodzą przez ten sam plan co zmiana po TAK, a w rozmowie widać je z dopiskiem
*bez pytania*. O usuwanie danych, dostęp do serwera (SSH, użytkownicy, zapora), autostart (cron, systemd), kod
i komendy, których nie rozpoznaje, pyta zawsze -- pełna lista: `docs/security.md` (*Samodzielność*). `AUTONOMY=0`
przywraca pytanie o każdą zmianę.

Każda zmiana dostaje **plan** -- przy ważnych widoczny w potwierdzeniu, zanim naciśniesz TAK:

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
| Kopia | pliki zmieniane przez `sed -i`, `tee`, `>`/`>>`, `cp`, `mv`, `rm`, `chmod`/`chown`, `write_file`; HEAD repozytorium przy `git pull/checkout/reset/commit`; crontab; pliki pamięci Pipe przy zmianie celów, rutyn, skilli i SERVER.md |
| Przed | walidacją konfiguracji przed (prze)ładowaniem: `nginx -t`, `sshd -t` (chroni przed odcięciem SSH), `caddy validate`, `apachectl configtest`, `haproxy -c`, `postfix check`, `docker compose config -q`; w kontenerze -- `docker exec <nginx> nginx -t`. **Nieudane sprawdzenie = operacja nie jest wykonywana** |
| Po | usługa `active`, kontener `running` i nie `unhealthy`, wszystkie kontenery projektu compose wstały, zmieniony plik przechodzi walidację (nginx, sshd, sudoers `visudo -c`, fstab, compose, JSON), **strony, które odpowiadały przed zmianą, odpowiadają po niej** |
| Przywrócenie | nieudana weryfikacja zmiany plików konfiguracji -> kopia wraca automatycznie (`SAFE_AUTO_ROLLBACK=1`), a usługa jest przeładowana ponownie. Inne operacje -- wynik trafia do agenta, a Ty możesz `/cofnij` |

Plan powstaje z analizy komendy w kodzie (bez LLM); wykonywany jest dokładnie plan, który widziałeś.
Program, którego nie ma w miejscu działania Pipe (np. `sshd` w kontenerze), jest pomijany i zgłaszany jako
*nie sprawdzono* -- wtedy plan mówi to wprost.

**Dziennik i cofanie.** `/dziennik` pokazuje ostatnie zmiany (`backend/data/journal/`, 50 wpisów, 14 dni).
`/cofnij` (albo *"cofnij ostatnią zmianę"*) pokazuje różnice plików i komendy odwrotne -- `docker stop` ->
`docker start`, `systemctl disable` -> `enable`, `git pull` -> `git reset --keep <poprzedni HEAD>`,
`docker compose down` -> `up -d` -- i czeka na TAK. `/cofnij` działa bez LLM, więc pomaga też wtedy, gdy
provider nie odpowiada albo skończył się dzienny limit. Cofnięcie też trafia do dziennika -- można je cofnąć.

Czego Pipe nie cofa automatycznie (i mówi to w planie): instalacji pakietów, zmian w klastrze Kubernetes,
usuniętych kontenerów i wolumenów, `git clean`, katalogów usuwanych `rm -r`, poprzednich wersji obrazów.

---

## Audyt bezpieczeństwa i strażnik

`/audyt` (albo *"jak bezpieczny jest ten serwer?"*) -- ocena 0-100 bez LLM, każdy punkt z poprawką:

| Obszar | Co sprawdza |
|--------|-------------|
| SSH | logowanie hasłem (z uwzględnieniem `sshd_config.d` i zasady "pierwsza wartość wygrywa"), root z hasłem, klucze |
| Zapora | ufw, firewalld, nftables, iptables (zapora w panelu dostawcy -- Pipe tego nie widzi i mówi to) |
| Porty | bazy danych, Redis, Elasticsearch, API Dockera/Kubernetesa na publicznym adresie; port publikowany przez kontener dostaje poprawkę w compose, bo **Docker omija ufw** |
| Kontenery | `privileged`, zamontowany `docker.sock`, sieć hosta |
| Konta | uid 0 poza rootem, konta bez hasła (z `/etc/shadow` odczytywane są tylko nazwy kont) |
| Aktualizacje | unattended-upgrades, oczekujące poprawki bezpieczeństwa, wymagany restart |
| Podatności | znane podatności z poprawką w pakietach systemu, obrazach kontenerów, zdalnych serwerach i aplikacjach (OSV.dev, CISA KEV) -- poniżej |
| Ochrona | fail2ban / CrowdSec (ważniejsze, gdy SSH przyjmuje hasła) |
| Sekrety | pliki `.env` czytelne dla wszystkich w katalogach aplikacji z DIRECTORY |
| Higiena | synchronizacja czasu, swap na małej maszynie, certyfikaty |

*"napraw 1"* -- agent wykonuje poprawkę zwykłym narzędziem, więc dostajesz ją do zatwierdzenia z planem
bezpiecznika (kopia, `sshd -t` przed przeładowaniem, `/cofnij`). Poprawka SSH trafia do własnego pliku
`sshd_config.d/00-pipe-*.conf`, żeby wygrać z ustawieniami cloud-init. Bez klucza w `authorized_keys` Pipe
**nie proponuje** wyłączenia haseł -- najpierw klucz (skill `utwardz-ssh`). W trybie docker komendy są oznaczone
*w powłoce hosta* (system plików hosta jest tylko do odczytu).

**Strażnik** (co `WATCH_INTERVAL`, bez LLM) porównuje konta, klucze SSH, programy SUID/SGID i konfiguracje
uwierzytelniania (sudoers, sshd, PAM, `/etc/passwd`, `ld.so.preload`). Nowe konto z uid 0, nowy klucz, nowy
SUID albo `ld.so.preload` -- alert krytyczny. Zmiany zrobione przez Pipe (są w dzienniku) nie alarmują. Aktualizacje
pakietów też nie: program SUID albo konfiguracja, których treść zgadza się z sumą z bazy pakietów (dpkg `md5sums`
i `Conffiles`, apk), to łatka, a nie podmiana. Binarka podmieniona po instalacji dalej daje alert.

**Log SSH** (`/var/log/auth.log` albo `/var/log/secure`, czytany przyrostowo): seria nieudanych logowań,
udane logowanie hasłem z adresu, który wcześniej zgadywał hasła, logowanie z nowego adresu.

**Podatności w pakietach** (Debian, Ubuntu, Alpine). Pipe czyta listę pakietów z bazy dpkg/apk (niczego nie
uruchamia) i pyta [OSV.dev](https://osv.dev) o znane podatności dla dokładnych wersji. Liczy się tylko to, co ma
poprawkę -- podatności bez poprawki Pipe pomija, bo zwykły Debian ma ich setki i nic się z nimi nie da zrobić.
Raport zaczyna się od "Co z tym zrobić": jedna linia na serwer, obrazy i aplikacje z konkretnym krokiem. Do OSV trafiają wyłącznie
nazwy i wersje pakietów; `VULN_INTERVAL=0` wyłącza skan.

- Kiedy: gdy zmieni się lista pakietów (nowa aplikacja, aktualizacja -- wtedy OSV dostaje tylko zmienione pakiety)
  i co `VULN_INTERVAL` (6 h) dla wszystkich, bo nowe podatności pojawiają się w starych wersjach. `/podatnosci`,
  `/audyt` i `/zalataj` czytają listę pakietów, obrazów i zależności od nowa przy każdym wywołaniu: zmienione
  idą do OSV od razu, całość najwyżej co 10 minut, więc to zwykle kilka sekund. Gdy skan trwa dłużej niż 45 s, widać
  ostatni wynik z godziną, z której pochodzi.
- Alert przychodzi tylko o **nowych pilnych**: aktywnie wykorzystywanych (katalog CISA KEV) albo krytycznych
  i wysokich. Alert przychodzi sam (Telegram, `pipe web`) i przy każdej podatności mówi zwykłymi słowami, czego
  dotyczy i co grozi, a numer CVE jest na końcu w nawiasie. Pipe najpierw czeka (do 2 minut) na wyjaśnienia modelu,
  bez nich wysyła opis z OSV. Znane podatności nie wracają po restarcie Pipe. Na końcu alertu jest to, co zrobi
  przycisk *Załataj*, i czego nie załata: poprawek tylko z Ubuntu Pro, kontenerów bez compose i kontenerów samego
  Pipe (te odświeża `/aktualizuj`). Reszta jest w porannym raporcie i w `/audyt`; pytanie do agenta o podatności też
  daje opis każdej z nich.
- W `/audyt` podatności są najwyżej trzema krótkimi punktami (KEV, krytyczne i wysokie, pozostałe) z nazwami
  pakietów i odesłaniem do `/zalataj`, które podbija tylko te pakiety. Punkt o aktywnie wykorzystywanych jest
  pierwszy, a pakiet, którego `/zalataj` teraz nie zainstaluje, ma przy nazwie powód (*tylko Ubuntu Pro*, *bez zmian
  po ostatniej łatce*). Czego nie dało się sprawdzić (np. zdalny serwer bez połączenia), jest w linii *Nie
  sprawdzono*. Pod audytem przychodzi osobna wiadomość z podatnościami: każdy pakiet z opisem najpoważniejszej.
  Alert i raport zawsze mieszczą się w jednej wiadomości Telegrama, a reszta jest policzona. W trybie docker Pipe
  instaluje łatki na hoście przez kontener pomocniczy (patrz *Komendy na hoście* w bezpieczeństwie); w Kubernetesie
  łatki węzła instalujesz w jego powłoce.
- **`/zalataj` łata od razu.** Samo `/zalataj` (albo przycisk *Załataj* przy alercie w Telegramie i `pipe web`) to
  zgoda na całą łatkę: Pipe nie pokazuje komend i nie pyta o każdą z osobna. Kolejno instaluje poprawki pakietów tego
  serwera, odświeża obrazy kontenerów z compose i łata zdalne serwery, z krokami na żywo. `/zalataj obrazy` robi tylko
  obrazy, `/zalataj serwer` tylko pakiety tego serwera, `/zalataj serwery` tylko zdalne serwery, `/zalataj web2` jeden
  z nich. Komendy składa serwer z bieżącego skanu, bez LLM. Każda część idzie przez bezpiecznik (strony przed i po,
  wpis w dzienniku); gdy weryfikacja po zmianie nie przejdzie, kolejnych części Pipe nie łata. Potem skanuje od nowa
  i mówi, co załatał, co zostało bez zmian i dlaczego, oraz co zostaje do zrobienia ręcznie. Łatanie działa w tle:
  zamknięcie klienta go nie przerywa, a wynik przychodzi wtedy jako wiadomość.
- Apt dostaje komplet porządków: najpierw dokończenie przerwanej instalacji (`dpkg --configure -a`), czekanie na
  blokadę zamiast błędu, gdy akurat działa unattended-upgrades, i usunięcie pobranych paczek oraz starych jąder, które
  apt sam uznaje za zbędne (zostają działające i najnowsze, więc `/boot` się nie zapełnia). Potem `--only-upgrade`
  tylko podatnych pakietów i to samo sprzątanie jeszcze raz. Jądro Pipe podbija przez metapakiet (`linux-image-generic`,
  `linux-image-amd64`...), bo nowe jądro to nowy pakiet. Po łatce sprząta też obrazy bez tagu, cache budowania starszy
  niż doba oraz cache npm, pip, yarn i composer, a wynik podaje, ile miejsca zwolnił.
- Pakietu, którego apt nie podbił, i obrazu, którego nic nie zmieniło, Pipe nie próbuje znowu przez dobę; raport mówi
  dlaczego. Poprawek tylko z Ubuntu Pro (ESM) nie próbuje wcale, dopóki subskrypcja nie jest włączona (`pro attach`).
  Kontenerów samego Pipe `/zalataj` nie odtwarza (to robi `/aktualizuj`).
- Pakietów systemowych nie da się wiarygodnie cofnąć (starej wersji często nie ma już w repozytorium), więc
  `/cofnij` ich nie obejmuje. Biblioteki i jądro działają po restarcie usług albo serwera: gdy system czeka na
  restart, raport to mówi.
- Jądro ma tysiące rekordów CVE, więc dla niego Pipe bierze tylko biuletyny dystrybucji (USN, DSA, DLA). Liczy się
  najnowsze zainstalowane jądro: stare zostają do autoremove i nie są już zgłaszane. Pierwszy skan trwa około
  minuty, kolejne korzystają z zapisanych szczegółów.
- Agent w rozmowie łata tym samym narzędziem (`security_patch`): na "załataj serwer" łata od razu, bez pytania
  o każdą komendę apt i docker (przy `AUTONOMY=0` jedno potwierdzenie z listą tego, co zostanie załatane).
- **Obrazy kontenerów.** Ten sam skan obejmuje pakiety w obrazach działających kontenerów (Debian, Ubuntu, Alpine):
  Pipe czyta ich bazę pakietów przez `/proc/<pid>/root` kontenera albo z warstw overlay, niczego w kontenerze nie
  uruchamiając. Jeden wynik na obraz, alert `vuln:images`. Obrazy bez systemu (distroless, scratch) są wymienione
  jako niesprawdzone. `/zalataj obrazy` najpierw zostawia poprzedni obraz pod tagiem `pipe-rollback-*` (sprzątanie go
  nie usunie, a `/cofnij` do niego wraca), potem pobiera nowsze obrazy z rejestru pod tym samym tagiem
  (`pull --ignore-buildable`) i przebudowuje usługi z `build:` na aktualnym obrazie bazowym (`build --pull`). Gdy
  `FROM` w Dockerfile takiej usługi ma przypiętą wersję, a w tej samej linii jest nowsza (`node:20.11-slim` ->
  `node:20.20-slim`, `postgres:15.4` -> `postgres:15.19`; zmienia się tylko ostatnia liczba, nigdy wersja główna),
  Pipe najpierw podbija ten tag (tagi z Docker Hub, kopia Dockerfile w dzienniku -- `/cofnij` ją przywraca). Tag
  pływający (`node:20`), obraz przypięty skrótem, `FROM` ze zmienną i rejestry inne niż Docker Hub zostają bez zmian. Obraz,
  który nie ma nowszej wersji (przypięty tag, obraz bazowy jeszcze nieprzebudowany), dostaje poprawki z repozytorium
  swojego systemu: kontener tymczasowy z tego obrazu instaluje je jako root, a `docker commit` zapisuje wynik pod tym
  samym tagiem z jego użytkownikiem, ENTRYPOINT i CMD (jak `copa patch`). Potem `up -d` tylko tych usług i weryfikacja
  kontenerów projektu. Kontenery bez compose dostają tylko podpowiedź.
- **Zdalne serwery.** Cele ssh z `target_manage` są w tym samym skanie: Pipe czyta ich bazę pakietów jednym
  połączeniem ssh (os-release, dpkg/apk, reboot-required), a `/zalataj` instaluje na nich poprawki tymi samymi
  krokami apt, jako root albo przez `sudo -n` (sudo z hasłem nie zadziała, wynik mówi, co dodać w sudoers). Serwer bez
  połączenia jest wymieniony w raporcie, alert `vuln:servers` przychodzi o nowych pilnych.
- **Zależności aplikacji.** Pipe szuka lockfile'i w katalogach z DIRECTORY, w katalogach projektów compose
  działających kontenerów i w repozytoriach git pod `/root`, `/home`, `/srv`, `/opt`, `/var/www` i `/data`, razem
  z podkatalogami (np. `backend/`, `frontend/`), bez `node_modules`, `vendor` i katalogów budowania. Czyta
  `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `requirements*.txt` (tylko `==`), `poetry.lock`, `Pipfile.lock`,
  `uv.lock`, `go.mod`, `Cargo.lock`, `composer.lock`, `Gemfile.lock`. Wynik jest w `/audyt` z wersją, która usuwa
  podatności; alert `vuln:apps` tylko dla aktywnie wykorzystywanych i krytycznych, z przyciskiem *Załataj*.
- **Łatki zależności aplikacji** (`/zalataj`, samo to: `/zalataj aplikacje`). Pipe podbija podatne pakiety w plikach
  zależności menedżerem danego języka, uruchomionym w jednorazowym kontenerze (tylko katalog aplikacji, bez
  `docker.sock` i uprawnień, jako właściciel katalogu, bez skryptów instalacyjnych pakietów):

  | Plik | Co robi Pipe |
  |------|--------------|
  | `package-lock.json` | zależności z `package.json`: `npm install <pakiet>@<wersja> --package-lock-only` (dokładne przypięcie zostaje dokładne); przechodnie: `npm audit fix --package-lock-only` |
  | `yarn.lock` (yarn 2+), `pnpm-lock.yaml` | `yarn up` / `pnpm update` tylko w pliku blokady |
  | `requirements*.txt` | nowa wersja po `==` w tej samej linii (plik z `--hash` zostaje -- hashe by nie pasowały) |
  | `poetry.lock`, `uv.lock` | `poetry update --lock`, `uv lock --upgrade-package` |
  | `go.mod`, `Cargo.lock` | `go get <modul>@v<wersja>` + `go mod tidy`, `cargo update -p --precise` |
  | `composer.lock`, `Gemfile.lock` | `composer update --no-install --no-scripts --no-plugins`, `bundle lock --update` |

  Kopia zmienianych plików trafia do dziennika (jeden wpis na aplikację), a gdy z katalogu buduje się projekt compose,
  Pipe robi `build` i `up -d` z weryfikacją kontenerów -- `/cofnij` przywraca pliki i przebudowuje projekt. Czego
  menedżer nie podbił (ograniczenie wersji w `package.json`, `pyproject.toml`, `composer.json`...), pokazuje wynik i
  `/podatnosci`; Pipe nie próbuje tego znowu przez dobę. Ręcznie zostają: yarn 1 (nie umie zmienić samego
  `yarn.lock`), `Pipfile.lock` i wersja samego Go.
- **`/podatnosci`** pokazuje wszystko w jednym miejscu: pakiet pogrubiony, wersja i wersja z poprawką,
  pod nim podatności z wagą i krótkim wyjaśnieniem w języku Pipe -- co atakujący może zrobić i kiedy to grozi.
  Wyjaśnienia pisze model (`WORKER_MODEL`) na podstawie opisu z OSV, raz na podatność, i Pipe je zapamiętuje;
  bez modelu widać oryginalny opis z OSV. W `pipe web` raport jest w kartach (serwer, zdalne serwery, obrazy,
  aplikacje) z jednym przyciskiem *Załataj*, a łatanie pokazuje kroki na żywo i od razu stan po łatce.

---

## Pierwsze 5 minut

Po instalacji, gdy bot Telegram połączy się pierwszy raz, Pipe sam pisze: mapa serwera jako obraz, ocena
bezpieczeństwa z trzema najważniejszymi poprawkami i lista tego, czego od teraz pilnuje. CLI pokazuje to samo
przy pierwszym połączeniu z danym serwerem. Bez LLM.

---

## Wbudowane skille

| Skill | Co robi |
|-------|---------|
| `nginx-vhost` | nowa domena w nginx jako reverse proxy + certyfikat Let's Encrypt |
| `swap` | plik swap z wpisem w fstab (też btrfs) |
| `fail2ban-ssh` | fail2ban z jailem SSH, bez blokowania własnego adresu |
| `backup-postgres` | nocny `pg_dump` z rotacja, wpis `[backup]` w DIRECTORY (czuwanie pilnuje świeżości) |
| `aktualizuj-kontener` | pull + up usługi compose z planem powrotu do poprzedniego obrazu |
| `utwardz-ssh` | wyłączenie haseł w SSH w kolejności, która nie odetnie dostępu |
| `wolne-miejsce` | co zajmuje dysk i bezpieczne sprzątanie, krok po kroku |

Instalowane przy starcie do `backend/data/skills/` jak zwykłe skille -- masz je w `/skille` i jako komendy
(`/nginx_vhost`). Możesz je edytować i usuwać: nowa wersja z aktualizacji Pipe zastąpi tylko skill, którego
nie zmieniałeś, a usuniętego nie przywróci.

**Skille i pluginy z innych ekosystemów.** `/pobierz skill <wlasciciel/repo|URL> [filtr]` i
`/pobierz plugin <...>` (`/download`) biorą gotowe zestawy z repozytorium git -- ten sam format `SKILL.md` co Claude
Code, a pluginy w układzie pluginów Claude Code (`skills/`, `.mcp.json`, marketplace). Najpierw podgląd: które skille
trafia do pamięci (sam tekst, skrypty są pomijane), które serwery MCP dojdą (w izolowanym kontenerze, sekrety do
dopisania w `backend/.env` jako `MCP_...`) i co zostanie pominięte -- z powodem (za długi skill, serwer, który
potrzebuje plików pluginu, haki, agenty). Instalacja dopiero po *Zainstaluj*. Ta sama nazwa w kilku pluginach
dostaje przedrostek pluginu (`telegram-access`). W rozmowie: *"pobierz skille z anthropics/skills"*.

Przy `PIPE_LANG=en` instalowane są angielskie wersje: `update-container`, `harden-ssh`, `free-disk-space`
(pozostałe cztery mają tę samą nazwę w obu językach).

---

## Pamięć incydentów

Każdy rozwiązany alert zostaje w `backend/data/incidents.json`: co się stało, kiedy i jak długo, co ustalił agent
po *Zbadaj* (albo worker przy alercie z webhooka) i co pomogło -- zmiany z dziennika wykonane w czasie trwania
problemu. Bez dodatkowych zapytań do LLM.

Gdy ten sam problem wraca, alert na Telegramie ma linię **Poprzednio: 2026-09-12 (40 min) — ustalenia: logi
nginx bez rotacji... — pomogło: journalctl --vacuum-size=200M**, a polecenie *Zbadaj* dostaje tę historię: agent
najpierw sprawdza, czy to ta sama przyczyna, i proponuje sprawdzoną naprawę (dalej do zatwierdzenia).
`/incydenty` pokazuje historię; agent sięga po nią sam (*"czy to już się zdarzało?"*).

---

## Alerty z zewnątrz (webhooki)

Pipe dołącza do monitoringu, który już masz, zamiast go zastępować:

```bash
# backend/.env
WEBHOOK_PORT=7380
WEBHOOK_TOKEN=$(openssl rand -hex 24)
```

| Źródło | Adres | Konfiguracja po stronie nadawcy |
|--------|-------|---------------------------------|
| Prometheus Alertmanager | `/hook/alertmanager` | `webhook_configs: - url: http://127.0.0.1:7380/hook/alertmanager` + `http_config.authorization.credentials: <token>` |
| Grafana | `/hook/grafana` | contact point typu Webhook, nagłówek `Authorization: Bearer <token>` |
| Uptime Kuma | `/hook/uptime-kuma?token=<token>` | powiadomienie typu Webhook (application/json) |
| GitHub | `/hook/github` | webhook repozytorium, *Secret* = token; zdarzenia *Workflow runs*, *Deployment statuses* |
| Wszystko inne | `/hook/generic` | `{"title", "message", "severity", "status": "firing|resolved", "name"}` |

Alert trafia do czuwania (znika po `resolved`) i na Telegram z przyciskiem *Zbadaj*. Z `WEBHOOK_INVESTIGATE=1`
nowy alert od razu bada worker (tylko odczyty: usługi, kontenery, logi, zasoby, zmiany z ostatniej doby), tak samo
jak problemy wykryte przez Pipe: ustalenia przychodzą chwilę po alercie z przyciskiem *Napraw* -- ten sam alert
najwyżej raz na 6 h, 10 badań z webhooków dziennie (osobno od `AUTO_INVESTIGATE_DAILY`).

W Dockerze port jest publikowany tylko na `127.0.0.1:7380`; nadawcy spoza serwera -- przez odwrotne proxy z TLS.

---

## Załączniki: pliki, zrzuty ekranu, logi

Agentowi można wysłać plik razem z pytaniem: zrzut ekranu z błędem, log, konfigurację nginx. Na Telegramie
zdjęcie albo dokument (podpis to pytanie, album kilku zdjęć idzie jako jedna wiadomość), w `pipe web` spinacz,
przeciągnięcie pliku na okno albo wklejenie zrzutu ze schowka (miniatury przed wysłaniem), w CLI `/plik <sciezka>
[pytanie]` albo `@ścieżka` w zwykłej wiadomości (Tab podpowiada ścieżki).

Backend (`core/attachments.py`) rozpoznaje plik po zawartości, nie po nazwie:

- obraz (PNG, JPEG, GIF, WebP) idzie do modelu jako obraz i zostaje w historii tylko na jedną turę -- potem
  zastępuje go znacznik, żeby kolejne pytania nie wysyłały go od nowa. Gdy model nie przyjmuje obrazów (np. część
  modeli Ollamy), Pipe mówi to wprost: *model nie widzi obrazów* i odpowiada bez obrazu,
- tekst (UTF-8) jest wklejany do wiadomości po redakcji sekretów i przycięciu do 24 tys. znaków,
- reszta (archiwa, binarki, PDF) trafia do `DATA_DIR/uploads/` (0600, sprzątane po 7 dniach), a agent dostaje
  ścieżkę; przeniesienie pliku gdzie indziej to zwykła zmiana z potwierdzeniem.

Limity: 10 MB na plik, 20 MB i 10 plików na wiadomość. Treść załącznika to dane, nie polecenia: tryb YOLO jest
wstrzymany do następnej wiadomości, tak jak po przeczytaniu strony z internetu.

## Wiadomości głosowe

> Eksperymentalne: działa i ma testy, ale ma za sobą mniej użycia na prawdziwych serwerach niż reszta Pipe.
> Jeśli coś nie gra, zgłoś to w issue.

Na Telegramie wystarczy nagrać wiadomość: *"sprawdź, czemu sklep nie działa"*. Bot odpisze *Usłyszałem: ...*
i przekaże tekst agentowi. W `pipe web` jest przycisk mikrofonu: nagranie (do 5 minut) przeglądarka zamienia na
WAV 16 kHz i wysyła do transkrypcji, a tekst idzie do agenta. Transkrypcja: przy `LLM_PROVIDER=gemini` robi ją
sam model Gemini (wejście audio), przy `openai` albo `groq` endpoint Whisper tego providera -- w obu przypadkach
bez konfiguracji. Przy innych providerach ustaw `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` (np. darmowy Groq
`whisper-large-v3-turbo` albo lokalny faster-whisper); `STT_*` ma zawsze pierwszeństwo.

---

## Role i wielu użytkowników

| Rola | Może | Nie może |
|------|------|----------|
| admin | wszystko | -- |
| viewer | rozmowa, diagnoza, odczyty, wykresy, raporty, audyt, alerty | zatwierdzać zmian, `/cofnij`, zapisywać pamięci Pipe (SERVER.md, skille, cele, rutyny) |

- Telegram: `TELEGRAM_ALLOWED_USER_IDS` (admini) i `TELEGRAM_VIEWER_IDS` (tylko odczyt, z `AGENT_VIEWER_TOKEN`).
- CLI i inne klienty: osobny token na osobę albo laptop, odwoływalny:
  `python3 -m backend.tokens add laptop-kuba --role admin`, `list`, `revoke laptop-kuba`.
- Role egzekwuje backend. Sesja należy do tokenu, który ją założył.

---

## Interfejs webowy i schemat na żywo

`pipe web` otwiera w przeglądarce interfejs z rozmową i panelem bocznym. Działa na Twoim komputerze, przez ten sam
tunel co CLI.

**Pulpit** (pierwsza zakładka panelu) odpowiada na pytanie *"czy wszystko działa?"* bez modelu: nagłówek zwykłymi
słowami (*"Wszystko działa"* albo *"Wymaga uwagi: ..."*), pierścienie procesora, pamięci i dysku z przebiegiem
ostatniej doby, skrzynka *Od Pipe* (wiadomości z przyciskami akcji, nowe oznaczone kropka; dzwonek w górnym pasku
pokazuje, ile ich jest), karty projektów z warsztatu (logi, zatrzymanie, *Z GitHuba*) i zużycie modelu dziś (tokeny,
koszt, cache, ile Pipe zaoszczędził). Dzwonek przy skrzynce otwiera ustawienia powiadomień. Zasoby, skrzynkę i projekty
zwijasz klikiem w nagłówek sekcji (zwinięte zasoby zostają jako procenty w jednej linii), a pojedyncza wiadomość --
klikiem w jej tytuł. Przeglądarka to pamięta; dzwonek w górnym pasku i `/nowosci` rozwijają skrzynkę. Pusty ekran rozmowy
podsuwa zadania zwykłymi słowami (*Czy wszystko działa?*, *Zwolnij miejsce na dysku*, *Uruchom aplikację z GitHuba*,
*Napisz mi skrypt*); te oznaczone *bez AI* działają natychmiast i nic nie kosztują.

**Schemat** powstaje sam z tego, co Pipe wie o infrastrukturze -- nic się nie rysuje ani nie konfiguruje:

| Poziom | Co widać |
|--------|----------|
| Serwery | ten serwer i zdalne cele (SSH, kontenery, klastry) |
| Wnętrze serwera | internet, reverse proxy, projekty compose, pojedyncze kontenery, usługi i porty |
| Projekt | kontenery projektu, ich porty i zależności (aplikacja -> baza), katalog projektu |

- Gdy agent coś robi, element, którego to dotyczy, jest podświetlony, a po połączeniach płyną impulsy. Działanie
  czekające na Twoją zgodę ma inną, przerywaną obwódkę. Po zakończeniu element błyska na zielono albo czerwono.
- **Śledzę agenta** (domyślnie włączone) przenosi widok za agentem, także między poziomami. Dowolny własny ruch --
  przesunięcie, zoom, kliknięcie -- wyłącza śledzenie; przycisk włącza je z powrotem.
- Kliknięcie w element pokazuje jego stan, alerty oraz co agent robi na nim teraz i co robił ostatnio; w projekt
  albo serwer -- wchodzi poziom niżej.
- Pod schematem jest os czasu działań, z krokami bezpiecznika (kopia, sprawdzenie, weryfikacja) i workerów.

Przypisanie działania do elementu wynika z treści komendy: nazwa kontenera, jednostka systemd, ścieżka pliku, katalog
projektu. Gdy się nie da (np. dowolny skrypt), działanie jest pokazywane na całym serwerze. Wyszukiwanie i czytanie
stron (`web_search`, `web_fetch`, `software_info`) świeci na elemencie *Internet*.

**Na telefonie** rozmowa i pulpit przełączają się przyciskami w górnym pasku, a widok, język i motyw są w menu *...*. Schemat na wąskim ekranie układa się z góry na dół (czytelny bez przybliżania): jeden palec przesuwa, dwa przybliżają, podwójne stuknięcie w puste miejsce przybliża, przycisk z ramka wraca do całości. Szczegóły elementu wysuwają się od dołu. Pole pisania zostaje nad klawiaturą.

**Panel boczny** zajmuje domyślnie pół ekranu. Jego krawędź można przeciągnąć -- wężej albo szerzej, najwyżej do połowy
ekranu; dwuklik na krawędzi przywraca domyślna szerokość, a przeciągnięcie do prawej krawędzi okna chowa panel.
Panel chowa też przycisk w jego pasku zakładek; przycisk w górnym pasku chowa go i rozwija z powrotem. Ze schowanym
panelem rozmowa zajmuje całe okno (treść w czytelnej kolumnie pośrodku), a komenda albo powiadomienie, które otwiera
zakładkę (np. `/alerty`), rozwija panel sam. Szerokość i stan panelu zostają w przeglądarce. Z klawiatury: fokus na
krawędzi, strzałki zmieniają szerokość, Enter chowa.

**Kroki agenta w rozmowie.** Pod odpowiedzią widać, co agent zrobił: każda komenda (albo opis, np. *czyta
/etc/nginx/nginx.conf*) i kilka linii jej wyniku -- przy błędzie linie z błędem, przy sukcesie koniec. *Cały wynik*
rozwija do 60 linii. W trakcie pracy blok jest otwarty, po odpowiedzi zwija się do *Kroki: N* (chyba że coś się nie
udało albo czeka karta zgody). Zmiana wykonana bez pytania ma dopisek *bez pytania*. To ten sam wynik, który dostał
model, z ukrytymi sekretami. W widoku prostym bloków nie ma. CLI pokazuje to samo jako szare linie pod komendą, Telegram
w wiadomości postępu.

**Zmiany.** Przed zatwierdzeniem zapisu pliku widać różnice w karcie potwierdzenia. Po wykonaniu różnica "przed -> po"
pojawia się w rozmowie i w zakładce *Zmiany* -- także gdy plik zmieniła komenda (np. `sed`), bo bezpiecznik robi kopię.
Sekrety w różnicach są redagowane. Każdy wpis ma przycisk cofnięcia. Różnic nie ma dla katalogów i danych w bazach.

**Komendy i skille.** Wpisanie `/` w polu rozmowy rozwija listę komend (`/status`, `/nowosci`, `/raport`, `/audyt`...)
i skilli z opisami -- wybór strzałkami albo kliknięciem, Tab uzupełnia komendę, żeby dopisać argumenty. `/skille`
pokazuje zapisane procedury w rozmowie, z podglądem treści i przyciskiem uruchomienia. Panel ma trzy zakładki:
*Pulpit*, *Schemat* (kropka przy nazwie, gdy agent właśnie pracuje) i *Zmiany*; ostatnio wybrana zostaje w przeglądarce.

**Provider LLM.** Przy pierwszym wejściu strona pyta, z którym providerem ma pracować Pipe: wybierasz go z listy,
wklejasz klucz API (jest sprawdzany u providera, zanim zostanie zapisany) i wybierasz model z jego aktualnej listy.
Ekran można pominąć -- zostaje provider z instalacji. Nad polem rozmowy jest przełącznik pokazujący, kto odpowiada;
rozwija listę providerów, do których jest klucz, i przełącza bez restartu, w trakcie rozmowy. `/provider` otwiera
ekran ponownie (kolejny klucz, inny model, usunięcie klucza). Klucze leżą na serwerze w `DATA_DIR/llm_keys.json`
(0600) i nie wracają do przeglądarki. Wybór dotyczy całego agenta: po przełączeniu z nowego providera korzystają też
Telegram, CLI, rutyny i workery; `WORKER_MODEL` i ceny z `.env` dotyczą tylko providera bazowego. Kilka kluczy można
też podać od razu w `backend/.env` (`OPENAI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`...).

## Pipe w przeglądarce z telefonu i komputera (`/web`)

`pipe web` działa na Twoim komputerze przez tunel SSH. Żeby wejść do Pipe z telefonu albo z dowolnego komputera
bez tunelu, napisz w Telegramie, CLI albo `pipe web` jedną komendę:

```
/web wlacz
```

Pipe stawia przed sobą serwer HTTPS (Caddy w kontenerze `pipe-web`) i odpisuje adresem, np.
`https://203-0-113-9.sslip.io` -- to darmowa nazwa, która wskazuje na IP Twojego serwera, więc certyfikat Let's
Encrypt powstaje sam, bez kupowania domeny. Z własną domeną: `/web wlacz pipe.mojafirma.pl` (rekord DNS musi już
wskazywać na serwer). Port to 443, a gdy jest zajęty (np. przez nginx) -- 8443; własny: `/web wlacz 9443`.

**Na mikr.us** Pipe rozpoznaje serwer (plik `/klucz_api`) i korzysta z subdomen mikr.us: strona nasłuchuje na
IPv6, a mikr.us sam daje adres `https://<serwer>-7443.mikrus.cloud` z certyfikatem. Nic nie trzeba otwierać ani
zakładać, adres się nie zmienia, ruch nie idzie przez nikogo poza mikr.us. Wymuszenie: `/web wlacz mikrus`.

**Gdy dostawca nie wpuszcza ruchu na 80/443** (inne serwery na współdzielonym adresie, serwer w domu za
routerem, panel chmury z zablokowanymi portami), certyfikatu nie da się wystawić. Pipe sprawdza to sam: jeśli
publiczny adres nie jest adresem serwera albo certyfikat nie powstaje, przechodzi na **tunel Cloudflare** i odpisuje
adresem w rodzaju `https://quiet-river-1a2b.trycloudflare.com`. Tunel to kontener `cloudflared`, który sam łączy się
z Cloudflare -- nie trzeba otwierać żadnego portu ani zakładać konta. Cena: ruch przechodzi przez Cloudflare (to on
szyfruje połączenie z przeglądarką), szybki tunel nie ma gwarancji działania, powiadomienia na stronie przychodzą co
kilka sekund zamiast od razu, a adres zmienia się po restarcie serwera -- Pipe zauważa nowy i przysyla go w
wiadomości (urządzenia wpuszcza się wtedy jeszcze raz). Wymuszenie: `/web wlacz tunel` albo
`/web wlacz bezposrednio`.

Zanim Pipe poda link, sam go otwiera z internetu i sprawdza, czy odpowiada strona prośby o dostęp. Gdy nie, mówi
dlaczego i pokazuje ostatnie linie z logów Caddy albo `cloudflared`.

**Jak się wchodzi.** Strona nie ma hasła ani tajnego linku. Otwierasz adres, naciskasz *Poproś o dostęp* i widzisz
kod, np. `K7P-4QX`. W tym samym momencie Pipe pisze do Ciebie (Telegram, `pipe web` na innym urządzeniu, CLI):
*"Ktoś chce wejść do Pipe przez przeglądarkę -- iPhone, Safari, IP ..., kod K7P-4QX"* z przyciskami *Wpuść*,
*Tylko podgląd* i *Odrzuć* (albo piszesz `/web wpusc K7P-4QX`). Sprawdzasz, czy kod się zgadza, i naciskasz *Wpuść*
-- strona sama się otwiera. Każde urządzenie wpuszczasz raz: telefon osobno, komputer osobno. Potem pamięta
logowanie przez 30 dni od ostatniego użycia.

- `/web` -- adres, lista urządzeń (co to za urządzenie, pełny dostęp czy podgląd, kiedy ostatnio) i prośby czekające
  na zgodę.
- `/web odwolaj 2` albo `/web odwolaj wszystkie` -- urządzenie traci dostęp od razu, otwarta strona wraca do prośby
  o dostęp.
- `/web wylacz` -- zatrzymuje Caddy albo tunel i most, zamyka porty, które Pipe otworzył w ufw. Urządzenia zostają na wypadek
  ponownego włączenia.

Urządzenie wpuszczone przyciskiem *Tylko podgląd* ma role viewer: rozmawia, czyta raporty i schemat, ale niczego
nie zmienia. Wpuszczać może tylko administrator. Strona na telefonie to ten sam interfejs co `pipe web` -- rozmowa
i pulpit przełącza się przyciskami w górnym pasku. Dodana do ekranu głównego (Safari: *Udostępnij* → *Do ekranu
początkowego*, Chrome: *Dodaj do ekranu głównego*) ma ikonę Pipe.

Gdy na serwerze działa ufw, Pipe otwiera w nim port strony (i 80, gdy strona nie jest na 443 -- tam Let's Encrypt
sprawdza serwer przy wystawianiu certyfikatu). Zapory u dostawcy (panel chmury) Pipe nie widzi: jeśli strona nie
odpowiada, Pipe mówi, który port otworzyć. Działa w trybie docker i native (potrzebny Docker -- Caddy jest
kontenerem), nie w trybie obserwacji ani w Kubernetes (tam Ingress). Szczegóły zabezpieczeń: `docs/security.md`
(*Pipe w internecie*).

---

## Widoki: prosty i techniczny

Pipe ma dwa widoki, jak jedna aplikacja do dwóch rzeczy. **Prosty** jest dla osób nietechnicznych: rozmowa zwykłymi
słowami, przypomnienia, pliki, skille i dodatki do aplikacji zewnętrznych (serwery MCP: GitHub, kalendarz, poczta).
Agent sam wybiera sposób wykonania zadania, nie każe wpisywać komend, a o ważne sprawy pyta prostym pytaniem tak/nie.
**Techniczny** ma to samo i do tego serwer: schemat na żywo, dziennik zmian z cofaniem, podatności i łatanie, kontenery,
zdalne serwery.

Przy pierwszym uruchomieniu Pipe pyta, co chcesz robić. Można odpowiedzieć kilkoma słowami (*"przypomnienia i maile"*,
*"pilnować serwera ze sklepem"*) albo wybrać widok. Widok jest wspólny dla całego agenta, tak jak język: `/widok prosty`,
`/widok techniczny` (`/view`) w CLI, w `pipe web` (także przycisk w nagłówku) i na Telegramie (przyciski). Zmiana
w jednym kanale przełącza pozostałe. Zapis w `DATA_DIR/view.json`, zmienia tylko administrator. Dopóki nikt nie
wybrał, obowiązuje widok techniczny, czyli to, co Pipe pokazywał dotąd.

| Gdzie | Widok prosty | Widok techniczny |
|-------|--------------|------------------|
| `pipe web` | zakładki *Pulpit* (bez wykresów i projektów) i *Dodatki*, podpowiedzi na co dzień | *Pulpit*, *Schemat*, *Zmiany*, *Dodatki* |
| CLI | `/pomoc` i podpowiedzi `/` bez komend serwera | wszystkie komendy |
| Telegram | menu `/`, `/pomoc` i klawiatura: stan, nowości, skille, pomoc | menu i klawiatura jak dotąd |
| Agent | blok *WIDOK PROSTY* w prompcie: zwykłe słowa, bez zargonu | bez zmian |

Komendy serwera (`/zalataj`, `/mapa`, `/audyt` i inne) działają w obu widokach po wpisaniu, widok prosty tylko ich
nie podsuwa. Zakładka *Dodatki* w `pipe web` zbiera skille (uruchomienie, podgląd), podłączone serwery MCP i dodawanie
nowych z GitHuba (`/pobierz`, `/mcp dodaj`).

**Pierwsze uruchomienie bez serwera.** `pipe web` bez `--host` i bez zapisanego połączenia otwiera kreator: najpierw
widok, potem miejsce. *Na tym komputerze* uruchamia backend w Dockerze z tego repozytorium (na Linuksie Pipe zarządza
tym komputerem, na Windows i macOS pracuje we własnym kontenerze Docker Desktop, `backend/docker-compose.desktop.yml`).
*Na moim serwerze* łączy się przez SSH z adresem i hasłem albo kluczem, klonuje repozytorium do `/opt/pipe`
i uruchamia `install-server.sh` bez pytań (bez modelu; klucz API dodajesz potem na stronie). Hasło zostaje w procesie
`pipe web`: nie trafia na dysk, do backendu ani do modelu. Po haśle Pipe dopisuje na serwerze własny klucz
(`~/.pipe/keys`) i kolejne połączenia idą już nim. Połączenie zapisuje się w `~/.pipe/app.json`, więc następne `pipe web`
i zwykłe `pipe` łącza się same. Kreator wraca z `pipe web --setup` albo po kliknięciu adresu serwera w nagłówku.

---

## Język: angielski albo polski

`PIPE_LANG=en` (domyślnie) albo `PIPE_LANG=pl` przełącza cały produkt, nie tylko język odpowiedzi modelu.
Język można też zmienić w trakcie pracy, bez restartu: `/jezyk pl` / `/jezyk en` (`/language`) w CLI, w `pipe web`
(komenda albo przycisk z flagą w nagłówku) i na Telegramie. Wybór jest wspólny dla całego agenta -- pozostałe kanały
przełączają się same -- zapisuje się w `DATA_DIR/language.json` i obowiązuje do ręcznej zmiany `PIPE_LANG` w `.env`.
Zmienia tylko administrator.

| Warstwa | Co się zmienia |
|---------|----------------|
| Agent | system prompt, opisy 24 narzędzi, prompty workerów, rutyn, skanu serwera i VIBE |
| Komunikaty | pytania o potwierdzenie, plan bezpiecznika, wyniki narzędzi, błędy walidacji |
| Czuwanie | tytuły alertów, poranny raport, `/zmiany`, `/zdrowie`, `/audyt` z poprawkami, powitanie |
| MCP | tytuły i opisy narzędzi serwera MCP, instrukcje dla zewnętrznego agenta |
| Klienci | CLI (`--lang pl`; bez niego CLI i `pipe web` mówią językiem serwera), bot Telegrama (menu `/`, przyciski, etykiety), kreator i instalator |
| Skille | wbudowane skille w wybranym języku |

Znaczniki protokołu -- `[POTWIERDZ]`, `[BLAD]`, `[ODMOWA]`, `[PAMIEC]`, `[ZREDAGOWANO: ...]` -- są takie same
w obu językach: serwer rozpoznaje po nich status ramki, a klienci zamieniają je na etykiety w swoim języku.
Komendy mają angielskie aliasy (`/report`, `/changes`, `/undo`, ...), które działają obok polskich.

Dane zapisane wcześniej (SERVER.md, VIBE, własne skille, wpisy dziennika) zostają w języku, w którym powstały.

---

## MCP -- Pipe jako brama dla innych agentów i klient cudzych serwerów

### Pipe jako serwer MCP

Claude Code, Cursor albo własny agent dostaje narzędzia Pipe zamiast gołej powłoki na produkcji:

| Narzędzie | Co robi |
|-----------|---------|
| `run_command` | komenda przez klasyfikator Pipe: odczyt od razu; zmiana -> **zgoda administratora**, potem bezpiecznik i dziennik; zakazana -> odmowa |
| `get_approval` | stan zgody i wynik (tylko dla agenta, który o nią prosił) |
| `read_file` | plik hosta; pliki z sekretami nie są wydawane, sekrety w treści -- redagowane |
| `server_status`, `server_changes`, `infra_map`, `health_checks`, `security_audit`, `journal` | wiedza Pipe o serwerze |
| `ask_pipe` | pytanie do agenta Pipe (zna SERVER.md, DIRECTORY, historie) -- w roli tylko do odczytu |

Zgoda przychodzi na Telegram z komendą i planem bezpiecznika (kopia, `nginx -t`, weryfikacja) oraz przyciskami
*Zatwierdź* / *Odrzuć*; w CLI -- `/zgody`. Agent dowiaduje się o wyniku przez `get_approval`.

**Połączenie z laptopa** -- most stdio w CLI, przez ten sam tunel SSH i tokeny. Gotowa konfiguracja dla Claude Code,
Claude Desktop, Cursora, Codexa, Hermes Agent i OpenClaw: `pipe connect <aplikacja> --host root@serwer`
(`docs/cli.md`). Ręcznie:

```json
{"mcpServers": {"pipe": {"command": "pipe", "args": ["--mcp", "--host", "root@serwer"],
                         "env": {"AGENT_TOKEN": "<token>"}}}}
```

Osobny token dla agenta: `python3 -m backend.tokens add claude-code --role admin` (albo `--role viewer`:
agent tylko czyta, nie może prosić o zgody). **Na serwerze** -- endpoint Streamable HTTP: `MCP_PORT=7381`,
`http://127.0.0.1:7381/mcp`, token w `Authorization: Bearer`.

### Pipe jako klient MCP

Pipe korzysta też z cudzych serwerów MCP -- np. poczty, kalendarza, GitHuba albo Grafany -- jak z własnych narzędzi.

**Okno `/mcp`.** `/mcp` bez argumentów otwiera okno (jak `/mcp` w Claude Code): w CLI ramka wybierana strzałkami,
w `pipe web` osobny panel, na Telegramie lista z przyciskami. Widać w nim podłączone serwery ze stanem, a po wybraniu
-- co dokładnie działa, jakie ma narzędzia (które pytają o zgodę) i przyciski *Połącz ponownie*, *Wyłącz*, *Usuń*.

**Gotowe dodatki** (`backend/addons/`, nic nie jest włączone z góry):

| Dodatek | Co daje | Czego potrzebuje |
|---|---|---|
| `outlook` | poczta, kalendarz i kontakty konta Microsoft (outlook.com, hotmail) | nic -- po dodaniu napisz *"zaloguj mnie do Outlooka"*: link microsoft.com/devicelogin i kod, logowanie zostaje po restarcie |
| `microsoft365` | Outlook i Teams konta firmowego albo szkolnego | to samo logowanie kodem; niektóre firmy wymagają zgody administratora |
| `google` | Gmail (czytanie, szkice), Kalendarz (odczyt), Dysk, Dokumenty, Arkusze | własny klient OAuth w Google Cloud i refresh token -- okno prowadzi krok po kroku |
| `github` | repozytoria, issues, pull requesty, Actions | token GitHuba (fine-grained) |

Klucze wpisuje się w oknie `/mcp` w `pipe web` albo w CLI (ukryte pole). Leżą w `backend/data/mcp_secrets.json` (0600)
i nie trafiają do modelu ani do przeglądarki; Telegram kluczy nie przyjmuje. W rozmowie też działa: *"podłącz mi
Outlooka"* -- agent zna katalog (`mcp_manage catalog`) i dodaje dodatek po jednym TAK. Dodatki z logowaniem
(`state: true`) dostają własny wolumen Dockera na tokeny; usunięcie dodatku usuwa go razem z zapisanym logowaniem.
Nowy dodatek to jeden plik JSON -- opis w `backend/addons/README.md`.

Własny serwer, spoza katalogu:

```
/mcp dodaj github npx -y @modelcontextprotocol/server-github GITHUB_PERSONAL_ACCESS_TOKEN=${MCP_GITHUB_TOKEN}
/mcp dodaj fetch uvx mcp-server-fetch
/mcp dodaj docs https://mcp.example.com/mcp TOKEN=${MCP_DOCS}
/mcp                      # lista i stan;  /mcp usun github;  /mcp odswiez
```

Albo w rozmowie: *"podłącz serwer MCP GitHuba"* -- wtedy jedno TAK z podglądem tego, co Pipe uruchomi.

| | |
|---|---|
| Gdzie działa | `npx`, `uvx` i obraz Dockera (także `docker run ...` z `.mcp.json` innych aplikacji) -- zawsze w osobnym kontenerze: bez plików serwera, bez `docker.sock`, bez uprawnień (`--cap-drop ALL`, użytkownik `nobody`), z limitem pamięci i procesów. Serwer widzi tylko sieć. Serwery HTTP -- pod podanym adresem |
| Sekrety | tylko jako `${MCP_NAZWA}` -- wpisane w oknie `/mcp` albo w `backend/.env` (np. `MCP_GITHUB_TOKEN=...`); nie wpisuj tokenu w czat; trafia do kontenera jako zmienna, nigdy w argumentach |
| Zgody | narzędzie oznaczone przez serwer jako odczyt -- bez pytania; każde inne -- TAK z widocznymi argumentami |
| Tokeny modelu | narzędzia serwera to grupa `mcp:<nazwa>` ładowana na żądanie: zwykła rozmowa ich nie wysyła, wiadomość z nazwą serwera (*"na githubie..."*) je włącza |
| Wynik | treść z zewnątrz: w tej turze YOLO jest wyłączone; argument z sekretem nie wychodzi do serwera |
| Tryb obserwacji, viewer | tylko narzędzia-odczyty; nowego serwera nie da się dodać |

Konfiguracja: `backend/data/mcp_servers.json` (0600). Start serwera stdio trwa od kilkunastu sekund do minuty (`npx` /
`uvx` instalują pakiet w kontenerze); Pipe łączy się w tle po restarcie.

Obsługiwane wersje protokołu (w obie strony): 2026-07-28 (`server/discover`, wersja w `_meta`, nagłówki
`Mcp-Method`/`Mcp-Name`) i starsze z `initialize` (2025-11-25, 2025-06-18, 2025-03-26).

---

## Internet bez kluczy API

Agent sam sięga do sieci, gdy odpowiedź zależy od wiedzy spoza serwera -- nie trzeba go o to prosić ani
podawać żadnego klucza:

- `web_search` -- DuckDuckGo, a gdy nie odpowiada: Stack Exchange (Server Fault, Stack Overflow), potem Wikipedia.
- `web_fetch` -- czyta stronę; z pytaniem stronę czyta osobny model bez narzędzi i oddaje agentowi tylko odpowiedź.
- `software_info eol` -- czy wersja jest wspierana i do kiedy (endoflife.date); `software_info vulns` -- znane
  podatności pakietu w zainstalowanej wersji (OSV.dev), z wersją, która je naprawia.

Przykłady: *"nginx sypie upstream prematurely closed -- co to?"*, *"czy nasz PostgreSQL jest jeszcze wspierany?"*,
*"czy nginx na tym serwerze ma znane dziury?"*. Agent podaje źródła. Zabezpieczenia (sekrety, adresy spoza wyników,
sieć prywatna, wstrzymanie YOLO) opisuje `docs/security.md`. `WEB_SEARCH=off` wyłącza internet.

## Aktualizacja samego Pipe

Wpisz `/aktualizuj` (po angielsku `/update`) albo napisz agentowi "zaktualizuj się"; `/aktualizuj sprawdz` tylko
pokazuje wersje i to, czy jest nowsza. Agent sprawdza zdalne repozytorium i uruchamia
osobny kontener pomocniczy, który w katalogu instalacji wykonuje `git pull --ff-only`
i podmienia usługi Pipe na podpisany obraz wydania z GHCR (a gdy go nie ma, buduje je: `docker compose up -d --build`). Osobny kontener jest potrzebny, bo agent nie może
wymienić kontenera, w którym sam działa -- proces zginąłby w połowie.

- Backend jest niedostępny przez minutę lub kilka. Rozmowa wraca po restarcie (sesje leżą w `DATA_DIR/sessions`), ale
  operacja czekająca wtedy na TAK przepada i agent zaproponuje ją ponownie.
- Po restarcie przychodzi wiadomość z wynikiem do tego, kto zlecił aktualizację. Jeśli pobranie albo budowanie się
  nie uda, stara wersja działa dalej, a wiadomość zawiera koniec logu.
- Przebudowywane są tylko usługi, które już działały (bot Telegrama nie wystartuje, jeśli go nie używasz).
- Lokalne zmiany w plikach repozytorium mogą zablokować `git pull` -- agent ostrzega o nich przy sprawdzaniu.
- Tylko tryb Docker. W trybie native: `git pull` i `sudo systemctl restart pipe`; w Kubernetesie: nowy obraz.
