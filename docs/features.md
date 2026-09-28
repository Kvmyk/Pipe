# Workery, cele, rutyny, czuwanie i diagramy -- Pipe

Pipe v0.9.0

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
