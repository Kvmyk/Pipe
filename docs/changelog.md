# Historia zmian -- Pipe

## v0.45.0 (2026-10-26)

### Gotowe dodatki: Outlook, Microsoft 365 (Teams), Google, GitHub

- Nowy katalog dodatkow MCP (`backend/addons/`), nic nie jest wlaczone z gory: *Outlook* (konto prywatne: poczta,
  kalendarz, kontakty), *Microsoft 365* (konto firmowe: Outlook i Teams), *Google* (Gmail, Kalendarz, Dysk, Dokumenty,
  Arkusze) i *GitHub*. Dodaje sie je jednym kliknieciem albo w rozmowie (*"podlacz mi Outlooka"*)
- Outlook i Microsoft 365 nie potrzebuja kluczy: po dodaniu Pipe daje link microsoft.com/devicelogin i kod, a
  logowanie zostaje po restarcie (wlasny wolumen dodatku, usuwany razem z nim). Google wymaga wlasnego klienta OAuth
  i refresh tokenu -- okno prowadzi krok po kroku
- Klucze dodatkow wpisuje sie w oknie `/mcp` w `pipe web` albo w CLI; leza w `mcp_secrets.json` (0600), nie trafiaja
  do modelu, przegladarki ani audit logu. Nowy dodatek to jeden plik JSON (`backend/addons/README.md`)

### Okno `/mcp`

- `/mcp` bez argumentow otwiera okno jak w Claude Code: w CLI ramka wybierana strzalkami (Enter, Esc), w `pipe web`
  osobny panel, na Telegramie lista z przyciskami *Dodaj*. Widac stan serwerow, co dokladnie dziala, narzedzia
  (odczyt / pyta o zgode / bez pytania) i przyciski *Polacz ponownie*, *Wylacz*, *Usun*
- W zakladce *Dodatki* jest sekcja *Gotowe do dodania* i przycisk *Zarzadzaj*

### `/web` dziala tez u dostawcow blokujacych porty

- Gdy serwer jest za NAT-em albo na wspoldzielonym adresie (np. mikr.us), gdy porty 80 i 443 sa zajete albo
  certyfikat nie powstaje, `/web wlacz` przechodzi samo na tunel Cloudflare (`https://...trycloudflare.com`) -- bez
  otwierania portow i bez konta. Pipe mowi, dlaczego wybral tunel i co to znaczy (ruch przez Cloudflare, adres
  zmienia sie po restarcie -- nowy przychodzi w wiadomosci). Wymuszenie: `/web wlacz tunel` albo
  `/web wlacz bezposrednio`
- `pipe web` odbiera powiadomienia odpytywaniem, gdy proxy po drodze nie przepuszcza strumienia zdarzen (SSE)
- `/web` jest w menu komend Telegrama

## v0.44.0 (2026-10-26)

### Pipe w przegladarce z telefonu i komputera (`/web`)

- Nowa komenda `/web wlacz` (w CLI, Telegramie i `pipe web`) wystawia `pipe web` pod publicznym adresem HTTPS bez
  tunelu SSH. Przed Pipe staje Caddy (kontener `pipe-web`) z certyfikatem Let's Encrypt na nazwie
  `<ip-serwera>.sslip.io`, wiec domena nie jest potrzebna; z wlasna: `/web wlacz pipe.twojadomena.pl`. Port 443,
  a gdy zajety -- 8443
- Bez hasla i bez tajnego linku: strona pokazuje kod, a Pipe pyta w rozmowie, czy wpuscic urzadzenie (przyciski
  *Wpusc*, *Tylko podglad*, *Odrzuc* na Telegramie, w `pipe web` i w skrzynce CLI albo `/web wpusc <kod>`). Kazde
  urzadzenie wchodzi raz i pamieta logowanie przez 30 dni od ostatniego uzycia
- `/web` pokazuje adres i urzadzenia, `/web odwolaj <nr|wszystkie>` odcina urzadzenie od razu (takze otwarta karte),
  `/web wylacz` zatrzymuje Caddy i zamyka porty otwarte przez Pipe w ufw
- Urzadzenie to zwykly token klienta z rola admin albo viewer; tokeny urzadzen nie zamykaja trybu bez `AGENT_TOKEN`,
  wiec CLI i Telegram bez tokenu dzialaja dalej. Docker i native; nie w trybie obserwacji ani w Kubernetes

### `pipe web` na telefonie

- Pole wiadomosci ma 16 px na waskich ekranach (iPhone nie powieksza juz strony po dotknieciu), wysokosc strony
  uwzglednia pasek przegladarki, a podpowiedz w polu miesci sie w jednej linii

### Obraz Dockera

- Obraz backendu buduje sie z korzenia repozytorium i zawiera strone `pipe web` (potrzebna dla `/web`).
  `backend/Dockerfile.dockerignore` zastapil `backend/.dockerignore`; recznie budujacy obraz podaja
  `-f backend/Dockerfile .` zamiast `backend/`

## v0.43.0 (2026-10-26)

### Dwa widoki: prosty i techniczny

- Przy pierwszym uruchomieniu Pipe pyta, co chcesz robic. Odpowiedz kilkoma slowami (*"przypomnienia i maile"*,
  *"pilnowac serwera ze sklepem"*) albo wybor widoku; slowa rozpoznaje bez modelu, wiec dziala to przed dodaniem klucza
- Widok prosty: rozmowa zwyklymi slowami, przypomnienia, pliki, skille i dodatki do aplikacji (MCP). Agent pisze bez
  zargonu i sam dobiera sposob wykonania. Komendy serwera znikaja z podpowiedzi, `/pomoc` i menu, ale dzialaja po wpisaniu
- Widok techniczny: to samo plus schemat serwera, dziennik zmian, podatnosci, kontenery i zdalne serwery; obowiazuje,
  dopoki nikt nie wybierze, wiec istniejace instalacje wygladaja jak dotad
- Jeden widok dla calego agenta, jak jezyk: `/widok prosty|techniczny` (`/view`) w CLI, `pipe web` (przycisk
  w naglowku) i na Telegramie (przyciski, pytanie przy pierwszym `/start`, inne menu i klawiatura w widoku prostym).
  Zmiana w jednym kanale przelacza pozostale; zmienia tylko administrator
- `pipe web`: nowa zakladka *Dodatki* -- skille (uruchom, podglad), podlaczone serwery MCP i dodawanie nowych z GitHuba

### `pipe web` stawia Pipe sam

- `pipe web` bez `--host` otwiera kreator: najpierw widok, potem *Gdzie ma dzialac Pipe?*. *Na tym komputerze* --
  Docker z tego repozytorium (na Windows i macOS Pipe pracuje we wlasnym kontenerze Docker Desktop,
  `backend/docker-compose.desktop.yml`). *Na moim serwerze* -- adres i haslo albo klucz SSH; aplikacja klonuje
  repozytorium do `/opt/pipe` i uruchamia instalator bez pytan, postep widac na stronie
- Haslo zostaje w procesie `pipe web`: nie trafia na dysk, do backendu ani do modelu. Po pierwszym logowaniu Pipe dodaje
  na serwerze wlasny klucz (`~/.pipe/keys`) i dalej laczy sie nim; nowy klucz hosta zapisuje z odciskiem, zmieniony
  przerywa instalacje
- Polaczenie zapisuje sie w `~/.pipe/app.json`: kolejne `pipe web` i zwykle `pipe` bez `--host` lacza sie same.
  Kreator wraca z `pipe web --setup` albo po kliknieciu adresu serwera w naglowku
- Instalatory klienta (`install.sh`, `install.ps1`) nie wymagaja juz adresu serwera; klient potrzebuje `paramiko`
  (jest w `clients/cli/requirements.txt`)

## v0.42.1 (2026-10-26)

### Poprawki

- Telegram: `/mcp` i `/pobierz` pokazuja sie w menu `/` (w v0.42.0 dzialaly tylko wpisane recznie i byly w `/pomoc`)

## v0.42.0 (2026-10-26)

### Pipe dla innych agentow: `pipe connect`

- `pipe connect claude|claude-desktop|cursor|codex|hermes|openclaw --host root@serwer` wypisuje gotowa konfiguracje
  Pipe jako serwera MCP do wklejenia (Claude Code, Claude Desktop, Cursor, Codex, Hermes Agent, OpenClaw). Token
  idzie zawsze przez zmienna `AGENT_TOKEN`, nigdy w argumentach; bez `--token` podpowiedz, jak utworzyc osobny token
  dla tej aplikacji

### Pipe znow laczy sie z innymi serwerami MCP -- w izolacji

- `/mcp dodaj <nazwa> <npx ...|uvx ...|docker run ...|URL> [ZMIENNA=${MCP_X}]`, `/mcp`, `/mcp usun`, `/mcp odswiez`
  w CLI, Telegramie i `pipe web`; w rozmowie *"podlacz serwer MCP GitHuba"* -- jedno TAK z podgladem
- W v0.35 klient MCP zostal usuniety, bo cudzy serwer dzialal jako proces Pipe. Teraz serwer stdio (`npx`, `uvx`,
  obraz Dockera) dziala w jednorazowym kontenerze bez montowan, `docker.sock` i uprawnien, jako `nobody`, z limitami;
  inne programy i opcje `docker run` typu `-v`, `--network host`, `--privileged` sa odrzucane
- Sekrety tylko jako `${MCP_NAZWA}` z `backend/.env` (nie w czacie); tekst wygladajacy na sekret w konfiguracji --
  odmowa. Odczyty (`readOnlyHint`) dzialaja bez pytania, reszta po TAK z argumentami; wynik wylacza YOLO do nastepnej
  wiadomosci, argument z sekretem nie wychodzi; tryb obserwacji i viewer -- tylko odczyty
- Narzedzia serwera sa grupa `mcp:<nazwa>` ladowana na zadanie: zwykla rozmowa ich nie wysyla, wiadomosc z nazwa
  serwera je wlacza. Obraz serwera Pipe pobiera przed startem, a na start `npx`/`uvx` czeka do 3 minut

### `/pobierz`: skille i pluginy z innych ekosystemow

- `/pobierz skill <wlasciciel/repo|URL> [filtr]` i `/pobierz plugin <...>` (`/download`) biora skille w formacie
  `SKILL.md` i pluginy w ukladzie Claude Code (`skills/`, `.mcp.json`, marketplace) z repozytorium git
- Najpierw podglad: ktore skille trafia do pamieci (sam tekst ze zrodlem, skrypty pomijane), ktore serwery MCP dojda
  (przez izolacje wyzej, z lista zmiennych `MCP_...` do dopisania) i co zostanie pominiete z powodem; instalacja po
  *Zainstaluj*. Haki, komendy, agenty i programy z pluginow nie sa instalowane nigdy
- Repozytorium klonowane jak w warsztacie (bez hookow, submodulow, LFS, limit rozmiaru) i usuwane po odczycie;
  `SKILL.md` bedacy dowiazaniem do pliku spoza repozytorium jest pomijany. Powtarzajace sie nazwy skilli z kilku
  pluginow dostaja przedrostek pluginu
- Agent w rozmowie: `skill_manage install` (*"pobierz skille z anthropics/skills"*) -- ten sam podglad i jedno TAK

### Roadmapa

- Zdalne cele, workery, glosowki i Kubernetes zostaja w Pipe (etykieta "eksperymentalne" zniknie po uzyciu na
  prawdziwym serwerze)

## v0.41.2 (2026-10-26)

### Poprawki

- `/zalataj` nadal nie podbijal zaleznosci aplikacji na VPS-ach w kontenerze (LXC/OpenVZ), ktore dostaja mniej niz
  jeden pelny procesor: jadro odrzucalo limit `--cpus 1` ("cpu.cfs_quota_us: invalid argument", kod 125), a kontener
  menedzera pakietow w ogole nie startowal. Gdy Docker odrzuci limit procesora, Pipe od razu ponawia bez niego i do
  restartu nie ustawia go juz w kontenerach menedzera ani warsztatu. Limity pamieci i procesow zostaja

## v0.41.1 (2026-10-26)

### Poprawki

- `/zalataj` na serwerze z jednym procesorem nie podbijal zaleznosci aplikacji: kontener menedzera pakietow mial
  limit 2 procesorow, a Docker odrzuca limit wiekszy niz liczba procesorow serwera ("range of CPUs is from 0.01 to
  1.00"). Limit nie przekracza juz procesorow serwera -- takze w kontenerach warsztatu (`PROJECT_CPUS`)
- Wynik `/zalataj` nie podpowiada `/cofnij`, gdy podbicie zaleznosci nic nie zmienilo

## v0.41.0 (2026-10-26)

### `/zalataj obrazy` podbija `FROM` w Dockerfile

- Gdy usluga compose jest budowana z Dockerfile, ktory ma przypieta wersje obrazu bazowego, a w tej samej linii jest
  nowsza (`node:20.11-slim` -> `node:20.20-slim`, `postgres:15.4` -> `postgres:15.19`), Pipe przed przebudowa podbija
  ten tag. Zmienia sie tylko ostatnia liczba, nigdy wersja glowna, i tylko tag w linii `FROM`
- Dockerfile trafia do dziennika przed zmiana, `/cofnij` przywraca go razem z poprzednim obrazem. W trybie docker
  zapis idzie przez kontener pomocniczy (host poza `/root` jest w kontenerze tylko do odczytu)
- Bez zmian zostaja tagi plywajace (`node:20` -- te odswieza `build --pull`), obrazy przypiete skrotem, `FROM` ze
  zmienna i rejestry inne niz Docker Hub. Gdy w tej samej linii nowszej wersji nie ma, raport mowi, ze trzeba przejsc
  na nowsza wersje glowna

### Telegram

- `/aktualizuj` wrocilo do menu `/` (od v0.39 dzialalo tylko wpisane recznie i bylo w `/pomoc`)

## v0.40.0 (2026-10-26)

### `/zalataj` podbija zaleznosci aplikacji

- `/zalataj` (i przycisk *Zalataj* przy alercie `vuln:apps`) podbija podatne pakiety w plikach zaleznosci aplikacji;
  same aplikacje: `/zalataj aplikacje` (`/patch apps`). Obslugiwane: npm, yarn 2+, pnpm, `requirements*.txt`, poetry,
  uv, Go, Cargo, Composer i Bundler
- Menedzer pakietow dziala w jednorazowym kontenerze: widzi tylko katalog aplikacji, bez `docker.sock` i uprawnien,
  jako wlasciciel katalogu i bez skryptow instalacyjnych pakietow. Dokladnie przypieta wersja w `package.json`
  zostaje dokladna
- Pliki zaleznosci trafiaja do dziennika (jeden wpis na aplikacje), a projekt compose budowany z tego katalogu jest
  przebudowany i uruchomiony od nowa z weryfikacja kontenerow. `/cofnij` przywraca pliki i przebudowuje projekt
- Czego menedzer nie podbil (ograniczenie wersji w manifescie), wynik i `/podatnosci` kaza zmienic recznie; Pipe nie
  probuje tego znowu przez dobe. Recznie zostaja yarn 1, `Pipfile.lock` i wersja samego Go
- W trybie docker `/cofnij` przywraca tez pliki hosta spoza `/root` (w kontenerze sa tylko do odczytu) przez kontener
  pomocniczy

### Mniej narzedzi, jedna droga badania alertow

- Narzedzie `docker_manage` zniknelo: agent robi to samo przez `execute_command` (`docker ps`, `docker logs`), z tym
  samym klasyfikatorem i potwierdzeniami. Podstawowe narzedzia to teraz ok. 2,1 tys. tokenow przy kazdym kroku
- Alerty z webhookow (`WEBHOOK_INVESTIGATE=1`) bada ta sama droga co problemy wykryte przez Pipe: ustalenia przychodza
  z przyciskiem *Napraw* i zmianami z ostatniej doby. Ten sam alert najwyzej raz na 6 h, 10 badan z webhookow dziennie
  (osobno od `AUTO_INVESTIGATE_DAILY`)
- `/alerty`, `/zdrowie` i `/historia` zniknely z menu, pomocy i podpowiedzi (to samo pokazuja `/status`, `/nowosci`
  i `/dziennik`); wpisane recznie dzialaja dalej
- Schemat w `pipe web` znow podswietla operacje gita, a os czasu pokazuje rodzaj sprawdzenia sieci

## v0.39.0 (2026-10-26)

### Pipe pisze pierwszy

- Wszystko, co Pipe wysyla sam (alerty, podpowiedzi, ustalenia, raporty), trafia do skrzynki
  (`backend/data/feed.json`) i ma przyciski akcji: *Zbadaj*, *Zwolnij miejsce*, *Zalataj*, *Napraw*. Nic nie ginie,
  gdy nikt nie byl podlaczony. `/nowosci` (`/news`) we wszystkich klientach
- Podpowiedzi bez AI, co godzine (`INSIGHTS_INTERVAL`): kiedy zapelni sie dysk (z tempa przyrostu z 48 h), ile
  miejsca da sie odzyskac, kontener, ktory czesto sie restartuje, serwer czekajacy na restart, stale rosnacy RAM.
  Przyciski *Pozniej* (wraca za dobe) i *Mniej takich*
- Przy krytycznym alercie Pipe sam bada przyczyne (worker, tylko odczyty) i zaraz po alercie pisze, co ustalil, z
  przyciskiem *Napraw*. `AUTO_INVESTIGATE=critical|all|off`, najwyzej `AUTO_INVESTIGATE_DAILY` (4) badan dziennie
- Powiadomienia maja poziom (`NOTIFY_LEVEL`: wszystko / wazne / krytyczne), cisze nocna (`QUIET_HOURS`) i limit
  podpowiedzi dziennie (`INSIGHTS_PER_DAY`). Zmienisz je w trakcie przez `/powiadomienia`; reszta czeka w skrzynce
- `/sprzataj` (`/cleanup`) zwalnia miejsce bez AI: obrazy Dockera bez tagu, stary cache budowania, pobrane paczki
  apt i cache npm/pip/yarn/composer. Wynik mowi, ile zwolnil

### Warsztat: projekty, ktore Pipe stawia sam

- Nowe narzedzie `project`: *"postaw mi te aplikacje: https://github.com/..."* klonuje repozytorium, rozpoznaje bez
  AI, co to jest i jak to uruchomic (compose, Dockerfile, Node, Python, Go, PHP, strona statyczna), uruchamia je
  w kontenerze z portem na `127.0.0.1` i podaje adres. Projekty leza w `WORKSPACE_DIR` (`/root/pipe-workspace`)
- W warsztacie agent pisze pliki i uruchamia komendy bez pytania, ale zawsze w jednorazowym kontenerze bez
  `docker.sock` i uprawnien, z limitem pamieci, procesow i czasu. O zgode pyta raz przed uruchomieniem uslugi na
  stale i przed usunieciem projektu; wdrozenie jest w dzienniku, wiec `/cofnij` je usuwa
- Po przeczytaniu README sklonowanego repozytorium albo strony z internetu Pipe znow pyta o zgode na uruchomienie
  kodu, do Twojej nastepnej wiadomosci. `WORKSPACE_AUTONOMY=0` -- zgoda na kazda operacje
- `/projekty` (`/projects`) w CLI i na Telegramie, karty projektow z logami i zatrzymaniem w `pipe web`

### Mniej tokenow

- Narzedzia na zadanie (`TOOLS_MODE=lazy`, domyslnie): rozmowa zaczyna od podstawowych narzedzi, reszta wlacza
  sie sama, gdy wiadomosc jej potrzebuje, albo gdy model poprosi (`load_tools`). Ok. 2,4 tys. tokenow zamiast 6,2
  tys. przy kazdym kroku agenta. `TOOLS_MODE=all` przywraca stare zachowanie
- Wyniki narzedzi starsze niz dwie tury (`CONTEXT_KEEP_TURNS`) ida do modelu skrocone; historia w sesji zostaje
  pelna. Prompt systemowy jest krotszy
- `/status` nie pyta juz modelu: stan serwera przychodzi od razu i za darmo. `/koszt` i pulpit pokazuja, ile
  tokenow Pipe nie wyslal i ile wejscia przyszlo z cache providera

### Prostsze interfejsy

- `pipe web`: nowy *Pulpit* -- *Wszystko dziala* albo co wymaga uwagi, pierscienie procesora, pamieci i dysku z
  przebiegiem z doby, skrzynka *Od Pipe*, projekty, zuzycie modelu. Dzwonek liczy nowe wiadomosci. Zakladki *Skille*
  i *Alerty* zniknely (skille sa na liscie `/`, alerty w skrzynce); pusty ekran rozmowy podsuwa zadania zwyklymi
  slowami
- Telegram: stala klawiatura (*Stan serwera*, *Co nowego*, *Projekty*, *Pomoc*), krotsze menu `/`, ustawienia
  powiadomien przyciskami
- CLI: po polaczeniu pokazuje stan serwera, a wiadomosci od Pipe pojawiaja sie nad promptem; akcje wybierasz w
  `/nowosci`, wpisujac np. `1a`

## v0.38.0 (2026-10-17)

### `/zalataj` lata od razu

- `/zalataj` (i przycisk *Zalataj* przy alercie w Telegramie i `pipe web`) instaluje poprawki od razu, bez podgladu
  i bez pytania o kazda komende. Kolejno: pakiety tego serwera, obrazy kontenerow z compose i zdalne serwery. Kroki
  widac na zywo w jednej wiadomosci, na koncu krotki wynik: co zalatane, co bez zmian i dlaczego, ile miejsca
  zwolnione. `/zalataj obrazy`, `/zalataj serwer`, `/zalataj serwery` i `/zalataj web2` zawezaja latanie
- Latanie dziala w tle: zamkniecie klienta go nie przerywa, a wynik przychodzi wtedy jako wiadomosc. Drugie
  `/zalataj` w trakcie pokazuje postep tego samego latania
- Kazda czesc idzie przez bezpiecznik (strony przed i po, wpis w dzienniku). Gdy weryfikacja po zmianie nie
  przejdzie, kolejnych czesci Pipe nie lata
- Agent w rozmowie ma nowe narzedzie `security_patch`: "zalataj serwer" to jedno potwierdzenie z lista tego, co
  zostanie zalatane, zamiast pytania o kazda komende apt i docker (w YOLO bez pytania; viewer i tryb obserwacji
  go nie maja)

### Obrazy bez nowszej wersji i zdalne serwery

- Obraz, ktory pod swoim tagiem nie ma nowszej wersji (przypiety tag, obraz bazowy jeszcze nieprzebudowany), dostaje
  poprawki z repozytorium swojego systemu: kontener tymczasowy z tego obrazu instaluje je jako root, a
  `docker commit` zapisuje wynik pod tym samym tagiem z jego uzytkownikiem, ENTRYPOINT i CMD. Poprzedni obraz
  zostaje pod tagiem `pipe-rollback-*`, a `/cofnij` do niego wraca
- Zdalne serwery (cele ssh) sa w skanie podatnosci: Pipe czyta ich baze pakietow jednym polaczeniem ssh, a
  `/zalataj` instaluje na nich poprawki tymi samymi krokami co na tym serwerze, jako root albo przez `sudo -n`. Gdy
  sudo chce hasla, wynik mowi, co dopisac w sudoers. Nowy alert `vuln:servers` z przyciskiem *Zalataj*

### Latki nie zapychaja dysku

- Apt najpierw konczy przerwana instalacje (`dpkg --configure -a`), czeka na blokade, gdy akurat dziala
  unattended-upgrades, i usuwa pobrane paczki oraz stare jadra, ktore apt sam uznaje za zbedne (dzialajace
  i najnowsze zostaja, wiec `/boot` sie nie zapelnia). Potem podbija tylko podatne pakiety i sprzata jeszcze raz
- Po latce Pipe usuwa obrazy bez tagu, cache budowania starszy niz doba oraz cache npm, pip, yarn i composer
- Jadro Pipe podbija przez metapakiet (`linux-image-generic`, `linux-image-amd64`...), a liczy sie tylko najnowsze
  zainstalowane: stare, czekajace na autoremove, nie sa juz zglaszane jako podatne

### Zaleznosci aplikacji

- Pipe szuka lockfile'i takze w katalogach projektow compose dzialajacych kontenerow i w repozytoriach git pod
  `/root`, `/home`, `/srv`, `/opt`, `/var/www` i `/data`, razem z podkatalogami (`backend/`, `frontend/`), bez
  `node_modules`, `vendor` i katalogow budowania. Wczesniej tylko w katalogach z DIRECTORY i bez podkatalogow
- Nowe formaty: `yarn.lock` (v1 i berry), `pnpm-lock.yaml` i `uv.lock`

### Powiadomienia, `/podatnosci` i `/audyt` czytelniej

- Alerty o podatnosciach maja tytuly bez zlej odmiany ("Pilne podatnosci w pakietach serwera: 4, w tym aktywnie
  wykorzystywane"; jeden zdalny serwer z nazwy). Na koncu alertu jest to, co zrobi przycisk *Zalataj*, i czego nie
  zalata: poprawek tylko z Ubuntu Pro, kontenerow bez compose, kontenerow samego Pipe i obrazow, ktorych ostatnia
  latka nie zmienila. Alert o aplikacji podpowiada tylko narzedzia jej ekosystemu (npm nie dostaje pip i composer)
- Najgrozniejsze ida pierwsze (aktywnie wykorzystywane, krytyczne, wysokie) w alertach, `/podatnosci`, `/audyt`
  i porannym raporcie; wczesniej kolejnosc byla alfabetyczna. Jadro jest podpisane "linux (jadro)", a pakiet
  z poprawka tylko w Ubuntu Pro ma to przy nazwie
- "Co z tym zrobic" przy zdalnych serwerach mowi "Ten serwer" zamiast samego "Serwer", a kazda linia podaje komende,
  ktora zaweza latanie (`/zalataj web2`, `/zalataj obrazy`)
- W `/audyt` punkt o aktywnie wykorzystywanych jest pierwszy, poprawki sa krotkie, pakiet, ktorego `/zalataj` teraz
  nie zainstaluje, ma przy nazwie powod, a punkt bez niczego do zainstalowania nie ma "Napraw". Telegram pokazuje
  linie "Nie sprawdzono" (np. zdalny serwer bez polaczenia) -- wczesniej cisza wygladala jak "wszystko w porzadku".
  Agent dostaje z audytu narzedzie `security_patch` zamiast komendy apt, wiec "napraw 1" to jedno potwierdzenie
- Raport poranny pokazuje zdalne serwery z poprawkami i to, ze serwer czeka na restart, a liczby sa odmienione
  poprawnie ("4 pilne (1 aktywnie wykorzystywany)")

### Poprawki

- Mapa infrastruktury, wykresy i mapa w powitaniu dzialaja dalej: `mermaidx` jest przypiety ponizej 0.10. Wydana
  17.10 wersja 0.10.0 konczy renderowanie map i wykresow Pipe bledem `Unknown quickjs tag`, a obrazy Pipe budowane
  od nowa pobralyby wlasnie ja

### Protokol

- Komenda `patch` lata od razu: ramki `progress`, potem `data` z `text`, `summary`, `fixed`, `unchanged`, `freed`,
  `report` i `parts`. Pola `execute` i `patch_token` sa ignorowane, a klienci sprzed v0.38 dostaja wynik, z ktorym
  dalej sie da pracowac

## v0.37.1 (2026-10-14)

### Podatnosci i latki czytelniej we wszystkich kanalach

- `/podatnosci`, `/audyt` i alert zaczynaja sie od "Co z tym zrobic": jedna linia na serwer, obrazy i aplikacje,
  kazda z konkretnym krokiem (co zainstaluje `/zalataj`, co trzeba zmienic samemu i jak). Szczegoly pakietow sa pod
  spodem
- `/zalataj`, gdy nie ma czego instalowac, mowi to w pierwszym zdaniu, a potem tylko to, czego `/zalataj` nie
  naprawi. Zniknela liczba podatnosci bez poprawki ("czekaja na dystrybucje: 512") -- nic sie z nimi nie da zrobic
- Wynik instalacji latki w Telegramie i CLI jest krotki: status, weryfikacja bezpiecznika, wpis w dzienniku i co
  zostalo. Wczesniej przychodzilo 25 linii wyjscia `docker compose build` albo apt; teraz tylko przy bledzie i tylko
  koncowka, a calosc jest zwinieta w `pipe web`
- Telegram w trakcie instalacji pokazuje kroki bezpiecznika w jednej wiadomosci, aktualizowanej na zywo, zamiast
  kilku minut ciszy
- Obraz budowany z Dockerfile, ktorego latka nie zmienila, dostaje wlasciwy powod: obraz bazowy (`FROM`) nie ma
  nowszej wersji, trzeba ja podbic w Dockerfile. Wczesniej Pipe pisal "pod tym tagiem nie ma nowszego obrazu"
- Kontener bez compose nie jest juz opisywany jako "do odswiezenia przez /zalataj obrazy" -- Pipe nie ma jak go
  odswiezyc i teraz to mowi
- Poprawna odmiana liczebnikow ("1 pakiet", "2 pakiety", "5 pakietow"; "1 pilny", "18 pilnych"), sekcja, ktora sie
  nie zmiescila, jest wymieniona z nazwy zamiast pustego naglowka, opisy podatnosci sa zapisane jak reszta tekstow
  (bez "Atakujący" obok "Atakujacy"), a weryfikacja nie powtarza dwa razy "kontenery projektu dzialaja"

## v0.37.0 (2026-10-14)

### Podatnosci: po latce widac stan po latce

- `/podatnosci`, `/audyt` i podglad `/zalataj` przy duzej liczbie pakietow i obrazow zwykle pokazywaly stary wynik:
  kazde wywolanie pytalo OSV od nowa o wszystkie pakiety, co nie miescilo sie w 45 s, wiec wracal poprzedni skan.
  Teraz lista pakietow jest czytana przy kazdym wywolaniu, zmienione pakiety i obrazy ida do OSV od razu, a calosc
  najwyzej co 10 minut. Gdy Pipe musi pokazac starszy wynik, podaje jego godzine
- Ponowny skan po instalacji latki nie korzysta juz ze skanu, ktory ruszyl przed nia i widzial stare pakiety
  i kontenery
- Pakiet, ktorego apt nie podbil (poprawka tylko w Ubuntu Pro/ESM albo jeszcze nie ma jej w repozytorium), i obraz,
  pod ktorego tagiem nie bylo nowszego, nie wracaja do `/zalataj` przez dobe. Wczesniej Pipe proponowal w kolko to
  samo. Raport, podglad i audyt mowia, dlaczego ich nie lata, a wynik instalacji: co zalatal, a co zostalo bez zmian
- `/zalataj obrazy` nie odtwarza juz kontenerow samego Pipe (`up -d` przerywal Pipe w trakcie latania, wynik
  przepadal); te odswieza `/aktualizuj`
- Wyjasnienia podatnosci i znaczniki wyslanych alertow zapisane w trakcie skanu nie znikaja juz po jego koncu

### pipe web

- Podatnosci w kartach zamiast sciany tekstu: liczby dla serwera, obrazow i aplikacji (kazda z liczba pilnych),
  przyciski *Zalataj pilne*, *Wszystkie pakiety*, *Nowsze obrazy*, zwijane grupy, przy pakiecie waga, wersja ->
  poprawka i opis najpowazniejszej podatnosci (kolejne po kliknieciu)
- Podglad latki wyglada jak inne potwierdzenia: co zostanie zalatane, komenda, co zrobi bezpiecznik. Po
  *Zainstaluj* widac kroki bezpiecznika na zywo, potem wynik (zalatane / bez zmian, wynik komendy) i od razu aktualny
  stan; starsze raporty w rozmowie dostaja znacznik "Nieaktualne"
- Komendy `vulns`, `patch` i `audit` oddaja tez dane do tego widoku (`view`, `report`, `vulns_view`); Telegram i CLI
  dalej dostaja tekst

## v0.36.6 (2026-10-12)

### Poprawki

- `/podatnosci`, `/audyt` (takze audyt wywolany przez agenta) i podglad `/zalataj` skanuja od nowa przy kazdym
  wywolaniu: swieza lista pakietow, obrazow i zaleznosci oraz ponowne zapytanie do OSV. Wczesniej pokazywaly wynik
  czuwania sprzed nawet 12 godzin, wiec po latce albo przebudowie obrazu widac bylo stan sprzed zmiany. Szczegoly
  niezmienionych podatnosci sa w pamieci, wiec to zwykle kilka sekund; czuwanie w tle dalej skanuje co
  `VULN_INTERVAL` (6 h). *Zainstaluj* korzysta z wyniku, z ktorego powstal podglad

## v0.36.5 (2026-10-12)

### Poprawki

- `/zalataj obrazy` po aktualizacji Pipe dalej mowil "kontener bez compose": skan podatnosci zapisany na dysku wraca
  bez nowego zapytania do OSV, dopoki nie zmienia sie lista pakietow (do `VULN_INTERVAL`), i pamietal kontenery oraz
  etykiety compose z chwili skanu. Teraz `/zalataj` i zapisany skan biora kontenery i ich projekty compose z biezacego
  stanu Dockera — dziala tez po recznym odtworzeniu kontenera

## v0.36.4 (2026-10-12)

### Poprawki

- Agent pamieta, co pokazaly komendy bez modelu: wynik `/podatnosci`, `/zalataj` (podglad), `/audyt`, `/zdrowie`,
  `/zmiany`, `/alerty` i `/dziennik` trafia do rozmowy jako notatka. Wczesniej po `/zalataj obrazy` i "zrob to sam"
  agent widzial tylko poprzednia prosbe (np. `/aktualizuj`) i powtarzal ja. Prompt mowi mu, ze "zrob to sam" dotyczy
  tego, co uzytkownik zobaczyl, a latke z podgladu ma wykonac sam
- Kontenery postawione starym `docker-compose` (v1, nazwy w stylu `projekt_usluga_1`) nie wypadaja juz jako "bez
  compose": wzgledna sciezka pliku compose jest liczona od katalogu projektu, a bez etykiety plikow Pipe szuka
  w nim `compose.yaml` / `docker-compose.yml`. `/zalataj obrazy` obejmuje je tak jak projekty compose v2

## v0.36.3 (2026-10-12)

### Poprawki

- `/zalataj` mowil "nie ma podatnosci", choc `/audyt` i `/podatnosci` pokazywaly dziesiatki: samo `/zalataj` patrzylo
  tylko na pakiety serwera. Teraz lata to, co jest: gdy pakiety serwera sa czyste, a obrazy kontenerow da sie odswiezyc,
  od razu pokazuje podglad dla obrazow z *Zainstaluj*. Gdy nie ma nic do zainstalowania, mowi, gdzie sa podatnosci
  (obrazy bez compose z podpowiedzia, aplikacje z podatnymi zaleznosciami, liczba czekajacych na dystrybucje)
  i dlaczego ich nie lata
- `/zalataj obrazy` przebudowuje wlasne obrazy: uslugi z `build:` dostaja `docker compose build --pull` (na aktualnym
  obrazie bazowym), obrazy z rejestru `pull --ignore-buildable`, potem `up -d`. Wczesniej sam `pull` nic nie dawal
  obrazom budowanym z Dockerfile

## v0.36.2 (2026-10-12)

### Poprawki

- *Zainstaluj* w `/zalataj` nie dzialal na zadnym serwerze z `AGENT_TOKEN` (instalator zawsze go ustawia): klienci
  wpisuja token uwierzytelnienia w pole `token`, a latka uzywala tego samego pola na swoj token, wiec kazda instalacja
  konczyla sie bledem "lista pakietow zmienila sie od podgladu". Token latki ma wlasne pole `patch_token` (Telegram,
  CLI, `pipe web`, `docs/protocol.md`)
- `/zalataj obrazy` i `/zalataj pilne` w `pipe web` gubily zakres: most przegladarki nie przepuszczal pola `scope`,
  wiec backend zawsze sprawdzal pakiety serwera i odpowiadal "Nie ma podatnosci z poprawka", choc obrazy je mialy

## v0.36.1 (2026-10-12)

### Poprawki

- Po latce z `/zalataj` (i `/zalataj obrazy`) agent dostaje w rozmowie notatke z wynikiem: co instalowano, kod
  wyjscia, wpis w dzienniku, co zostalo. Wczesniej latka szla bez modelu, wiec zapytany "jak tam latanie?" zgadywal
  ze starej historii; bez notatki ma sprawdzic dziennik
- Aktualizacja pakietow nie daje juz serii alertow "Zmiana bezpieczenstwa": program SUID (sudo, passwd, mount...)
  albo konfiguracja, ktorych tresc zgadza sie z suma z bazy pakietow (dpkg `md5sums` i `Conffiles`, apk), to latka,
  a nie podmiana. Dotyczy tez `unattended-upgrades` i recznego `apt upgrade`. Binarka podmieniona po instalacji
  dalej daje alert
- Agent w trybie docker wie, ze `docker compose` (build, pull, up -d) na projektach hosta robi przez `execute_command`
  z `host: true`; wczesniej mowil, ze bez compose w kontenerze sie nie da i trzeba wejsc na serwer. Zakaz recznej
  aktualizacji przez compose dotyczy juz tylko samego Pipe

## v0.36.0 (2026-10-12)

### Przejrzystsze web, CLI i Telegram

- Prosba o zgode we wszystkich klientach ma jeden naglowek ("Potrzebuje Twojej zgody") i od razu komende; zniknelo
  powtarzane "Operacja wymaga potwierdzenia:". Temat zostaje tylko, gdy cos mowi (np. "Zapis pliku")
- `pipe web`: plan bezpiecznika to jeden wiersz na krok, a po decyzji karta zwija sie do werdyktu i komendy (plan pod
  "Szczegoly"). Szybkie akcje sa tylko na pustym ekranie. Lista zmian nie pokazuje "wykonano" przy kazdym wpisie
  i nazywa narzedzia po ludzku ("komenda", "zapis pliku"), a udane przebiegi tej samej rutyny lacza sie w jeden wpis
- CLI pyta o zgode po polsku `[t/n]` i przyjmuje "tak" -- wczesniej odrzucalo je komunikatem "Please enter Y or N".
  `/pomoc` jest w grupach (na co dzien, bezpieczenstwo, pamiec, ustawienia), alerty maja wage slowem i kolorem
- Telegram: wiadomosc "W toku" po zakonczeniu zamienia sie w "Wykonane kroki: N" ze zwinieta lista (wczesniej
  zostawala w czacie na zawsze), decyzja TAK/NIE dopisuje sie do tej samej wiadomosci, alerty maja kolorowy znacznik
  wagi, a pomoc i menu "/" sa w grupach, najczestsze komendy na gorze

### Poprawki

- Zoom kolkiem na schemacie w `pipe web` jest plynny: kamera nie cofa sie przy kazdym ruchu kolka i nie staje miedzy
  kolejnymi ruchami; w Firefoksie z myszka zoom dziala tak samo jak w Chrome
- Na waskim telefonie pasek gorny `pipe web` miesci sie bez obcinania przycisku nowej rozmowy, a godzina w osi
  aktywnosci nie lamie sie na dwie linie

## v0.35.0 (2026-10-12)

### Pipe sam wprowadza zmiany na hoscie, takze w trybie docker

- `/zalataj` dziala w trybie docker: latki pakietow i nowsze obrazy instaluje kontener pomocniczy z obrazu Pipe
  (`docker run --privileged --pid=host`, `nsenter` do procesu 1 hosta), jak root w powloce serwera. Tak samo ida
  sprawdzenia bezpiecznika (`docker compose config`, stan kontenerow) i komendy odwrotne dla `/cofnij obrazy`
- Agent robi zmiany na hoscie sam (`apt-get`, `systemctl`, `sed -i` w `/etc`, `ufw`): `execute_command` z `host: true`,
  zamiast kazac wpisywac komende recznie. Kazda, takze odczyt, czeka na TAK; w potwierdzeniu widac sama komende
  i linie "na hoscie: kontener pomocniczy...". Zakazane sa odrzucane, tryb obserwacji i rola viewer tej drogi nie maja
- Nie dochodzi nowe uprawnienie (`docker.sock` i tak daje roota na hoscie), ale "host tylko do odczytu" przestaje
  byc bariera -- zostaje Twoje TAK. Opis w `docs/security.md`, *Komendy na hoscie*

### Podatnosci w jednej, czytelnej wiadomosci

- Alert, `/podatnosci` i podglad `/zalataj` zawsze mieszcza sie w jednej wiadomosci Telegrama: kazdy pakiet z liczba
  podatnosci i opisem najpowazniejszej, reszta policzona ("… i 60 kolejnych pakietow")
- `/audyt` ma krotkie punkty o podatnosciach (nazwy pakietow, "Napraw: /zalataj pilne") zamiast akapitu opisow
  i komendy z setka pakietow; pod audytem przychodzi osobna wiadomosc z podatnosciami i ich opisami (Telegram,
  CLI, `pipe web`)
- Komenda w podgladzie latki jest skracana ("… (+150 pakietow)"); pelna trafia do dziennika

### Nowa rozmowa

- `/nowa` (`/new`) w Telegramie, CLI i `pipe web`: rozmowa od czystej karty, historia znika z pamieci i z dysku.
  Notatki o serwerze, katalogi, skille, styl i alerty zostaja -- sa w prompcie przy kazdym zapytaniu. Najbardziej
  przydaje sie w Telegramie, gdzie bot mial jedna niekonczaca sie rozmowe na uzytkownika
- Przycisk "Nowa rozmowa" w `pipe web` od razu usuwa stara rozmowe z backendu

### Usuniete: Pipe jako klient MCP

- Narzedzie `mcp_manage`, narzedzia `mcp__*` cudzych serwerow MCP, komenda `/mcp` i komenda protokolu `mcp_servers`.
  Cudzy kod podpiety pod agenta z dostepem do roota to duza powierzchnia ataku przy malym zysku. `mcp.json`, jesli
  istnieje, przestaje byc czytany. Pipe jako serwer MCP (`pipe --mcp`, endpoint HTTP, zgody) zostaje

### Poprawki

- `pipe web`: przyblizanie kolkiem na schemacie jest plynne -- kolejne ruchy kolka przesuwaja cel zamiast zatrzymywac
  kamere, punkt pod kursorem zostaje na miejscu, Firefox z myszka (przewijanie liniami) przybliza jak inne przegladarki

## v0.34.1 (2026-10-12)

### Repozytorium gotowe na publiczne wydanie

- Linki do strony projektu w README prowadzily do `kvmyk.github.io/pipe/` i konczyly sie bledem 404; strona jest
  pod `kvmyk.github.io/Pipe/` (GitHub Pages rozroznia wielkosc liter)
- Nowy naglowek w README i na stronie: "Admin, ktory nie spi i niczego nie rusza bez pytania", a pod nim zwykle
  wyjasnienie, czym jest Pipe
- Link do strony albo repo wklejony na LinkedIna czy komunikator pokazuje karte z obrazkiem (okno zgody na zmiane
  i odpowiedz "tak"), po polsku albo po angielsku
- Szablony zgloszen (blad, wynik modelu z piaskownicy) i pull requestow, kodeks postepowania (Contributor Covenant
  2.1), link do prywatnego zglaszania podatnosci przy zakladaniu issue
- Dependabot raz w miesiacu podbija akcje GitHuba; usuniety nieuzywany plik `standard.flf`

## v0.34.0 (2026-10-12)

### Alerty o podatnosciach z opisem kazdej z nich

- Alert o podatnosciach (pakiety serwera, obrazy kontenerow, zaleznosci aplikacji) przychodzi sam i przy kazdej
  podatnosci mowi zwyklymi slowami, czego dotyczy i co grozi; numer CVE jest na koncu w nawiasie. Nie trzeba juz
  wpisywac `/podatnosci`, zeby zobaczyc opisy
- Pipe najpierw czeka (do 2 minut) na wyjasnienia modelu, dopiero potem wysyla alert; bez modelu albo gdy nie zdazy,
  alert ma opis z OSV. Gdy tytul biuletynu nic nie mowi ("security update"), Pipe bierze tresc rekordu
- Pytanie do agenta o podatnosci daje opis kazdej z nich: audyt przekazuje modelowi pelny raport, a prompt kaze
  tlumaczyc, co grozi, zamiast podawac same numery CVE i wersje
- Kazdy aktywny problem przychodzi po aktualizacji raz jeszcze, juz w nowej formie

### Czytelniejszy wyglad

- Bez kursywy: w Telegramie alerty i `/audyt` to zwykly tekst z pogrubionymi naglowkami i lista z kropkami
- Wersje pakietow i numery podatnosci nie sa juz w czcionce kodu; najpierw waga slowem ("Krytyczna:"), potem opis
- `pipe web` pokazuje tresc alertu jak odpowiedz w czacie, CLI w `/alerty` wypisuje alert o podatnosciach w calosci

## v0.33.0 (2026-10-12)

### Podatnosci w obrazach kontenerow i aplikacjach

- Skan podatnosci obejmuje pakiety w obrazach dzialajacych kontenerow (Debian, Ubuntu, Alpine): Pipe czyta ich baze
  pakietow przez `/proc/<pid>/root` kontenera albo z warstw overlay, niczego w kontenerze nie uruchamiajac. Jeden
  wynik na obraz, nowy alert `vuln:images`; obrazy bez systemu (distroless) sa wymienione jako niesprawdzone
- Zaleznosci aplikacji z katalogow w DIRECTORY: `package-lock.json`, `requirements*.txt`, `poetry.lock`,
  `Pipfile.lock`, `go.mod`, `Cargo.lock`, `composer.lock`, `Gemfile.lock`. Wynik w `/audyt` z wersja, ktora usuwa
  podatnosci; alert `vuln:apps` tylko dla aktywnie wykorzystywanych i krytycznych. Ten sam CVE opisany kilka razy
  (GHSA, PYSEC) liczy sie raz
- `/zalataj obrazy` (i przycisk *Zalataj* przy alercie o obrazach) pobiera nowsze obrazy dla kontenerow z compose
  (`docker compose pull` + `up -d` tylko tych uslug) przez bezpiecznik; `/cofnij` wraca do poprzednich obrazow.
  W trybie docker gotowa komenda dla hosta, kontenery bez compose dostaja podpowiedz
- Pierwszy skan po aktualizacji zaczyna od nowa (nowy format pamieci skanu)

### Czytelne podatnosci z opisami

- Nowa komenda `/podatnosci` (`/vulns`) w CLI, Telegramie i `pipe web`: pakiety serwera, obrazy i aplikacje w jednym
  miejscu. Pakiet pogrubiony, wersje i kody podatnosci wyroznione, waga slowem
- Kazda pokazywana podatnosc ma krotkie wyjasnienie w jezyku Pipe -- co atakujacy moze zrobic i kiedy to grozi.
  Pisze je model (`WORKER_MODEL`) na podstawie opisu z OSV, raz na podatnosc, i Pipe je zapamietuje; bez modelu
  widac oryginalny opis z OSV. Tekst z internetu jest czyszczony z linkow i znacznikow
- Podglad `/zalataj` ma ten sam format i pyta wprost: *Zainstaluj* albo *Anuluj* we wszystkich klientach (w CLI
  wybor zamiast t/N)

### Poprawki

- `software_info` ostrzega agenta, gdy pyta o pakiet w innej dystrybucji niz system serwera (np. Ubuntu na
  Debianie 13), a opis narzedzia kaze brac ekosystem z `/etc/os-release`; skan pomija rekordy spoza ekosystemu pakietu

## v0.32.0 (2026-10-11)

### Podatnosci w pakietach

- Pipe sam sprawdza, czy w pakietach systemu (Debian, Ubuntu, Alpine) sa znane podatnosci: czyta baze dpkg/apk
  bez uruchamiania czegokolwiek i pyta OSV.dev o dokladne wersje (po pakiecie zrodlowym). Priorytet z katalogu
  CISA KEV (aktywnie wykorzystywane) i z oceny dystrybucji albo CVSS. Liczy sie tylko to, co ma poprawke;
  podatnosci bez poprawki sa liczba
- Skan bez LLM i bez kluczy: przy kazdej zmianie listy pakietow (OSV dostaje tylko zmienione) i co `VULN_INTERVAL`
  (domyslnie 6 h) dla wszystkich. `VULN_INTERVAL=0` wylacza skan; do OSV trafiaja wylacznie nazwy i wersje pakietow
- Jadro ma tysiace rekordow CVE, wiec dla niego Pipe bierze tylko biuletyny dystrybucji (USN, DSA, DLA); rekord CVE
  objety biuletynem nie jest liczony drugi raz. Pierwszy skan trwa okolo minuty, kolejne korzystaja z zapisanych
  szczegolow
- Alert `vuln:system` przychodzi tylko o nowych pilnych podatnosciach (KEV, krytyczne, wysokie), z lista pakietow
  i komenda; znane nie wracaja po restarcie Pipe. Reszta jest w porannym raporcie
- `/audyt` pokazuje podatnosci jako najwyzej trzy punkty (KEV, krytyczne i wysokie, pozostale) z komenda
  `apt-get install --only-upgrade ...`, ktora podbija tylko te pakiety

### Latki na potwierdzenie

- Nowa komenda `/zalataj [pilne]` (`/patch`) w CLI, Telegramie i `pipe web`, a przy alercie o podatnosciach przycisk
  *Zalataj* (w Telegramie tylko dla administratorow). Dziala bez LLM: podglad z lista pakietow i dokladna komenda,
  potem *Zainstaluj*
- Komende sklada serwer z ostatniego skanu, klient przysyla tylko token z podgladu; gdy lista sie zmienila, nic sie
  nie instaluje. Instalacja idzie przez bezpiecznik (strony przed i po, wpis w dzienniku), potem Pipe skanuje
  zmienione pakiety i mowi, co zostalo
- Sam instaluje tylko w trybie native. W trybie docker (system hosta tylko do odczytu), w trybie obserwacji
  i dla roli viewer jest podglad z komenda do wykonania na serwerze. Pakietow nie cofa automatycznie i mowi to
  w podgladzie

## v0.31.1 (2026-10-10)

### Porzadki

- Zamrozenie funkcji do czasu pierwszych uzytkownikow spoza autora; priorytety w `docs/roadmap.md`
- Dokumentacja oznacza jako eksperymentalne: zdalne cele, workery, wiadomosci glosowe, Pipe jako klient MCP
  i wdrozenie w Kubernetesie
- Usuniety placeholder klienta Discord (`clients/discord/`) — obiecywal cos, czego nie ma
- Nowy `CONTRIBUTING.md` (po angielsku): w czym pomoc jest teraz najcenniejsza, jak uruchomic testy, zasady zmian

## v0.31.0 (2026-10-10)

### Piaskownica: wyprobuj Pipe bez serwera

- `python3 sandbox/run.py try` uruchamia na wlasnym komputerze kontener udajacy maly serwer (nginx, aplikacja sklepu,
  bez systemd), a w nim Pipe. Laczysz sie przez `pipe --no-tunnel --local-port 7390` albo `pipe web`. Bez klucza
  piaskownica tez startuje, Pipe dziala wtedy bez modelu
- Cztery scenariusze awarii (`--scenario`): `nginx-502`, `app-down`, `static-403`, `disk-hog`

### Ocena modeli na scenariuszach

- `python3 sandbox/run.py eval` daje kazdy scenariusz agentowi, zgadza sie na proponowane zmiany i sprawdza
  skryptem, czy serwer naprawde dziala. Wynik: liczba zdanych, potwierdzenia, czas; szczegoly w `sandbox/results/`.
  `--repeat` do porownan (modele nie sa deterministyczne)
- `python3 sandbox/run.py self-test` sprawdza bez modelu, ze scenariusze psuja sie i naprawiaja poprawnie; uruchamia
  go CI

### Bezpieczenstwo

- Nowy `.dockerignore` w korzeniu repozytorium. Wczesniej obraz bota Telegram budowany na serwerze (`--build` albo
  brak obrazu wydania) zawieral `clients/telegram/.env` z tokenem bota. Obrazy z ghcr.io byly czyste (CI buduje
  z repozytorium bez `.env`). Jesli budowales obraz bota lokalnie i gdzies go wypychales, zmien token bota

## v0.30.1 (2026-10-10)

### Fuzzing klasyfikatora komend

- Nowy `backend/tests/test_security_fuzz.py` (hypothesis): tysiace wariantow prob obejscia — doklejona komenda
  po rozpoznanym odczycie, przekierowanie do pliku, nazwa komendy rozcieta cudzyslowami, zmiany wielkosci liter
  i bialych znakow, odczyt sekretu ze sciezka w cudzyslowach. Zadna nie moze byc `safe`
- Przy 5000 przykladach na test nie znalazl obejscia; celowo oslabiony klasyfikator (dodany `touch`) wylapuje
  od razu. W CI 300 przykladow, glebiej: `PIPE_FUZZ_EXAMPLES=5000`

## v0.30.0 (2026-10-10)

### Rozmowa przetrwa restart backendu

- Po kazdej turze sesja trafia do `DATA_DIR/sessions` (prawa 0600, nazwa pliku z hasha identyfikatora). Po restarcie
  (`/aktualizuj`, awaria, restart kontenera) CLI, `pipe web` i Telegram wracaja do tej samej rozmowy: historia,
  katalog roboczy, historie workerow i adresy z wyszukiwania sa na miejscu
- Operacja, ktora w chwili restartu czekala na TAK, nie jest wykonywana: model dostaje odpowiedz „nie wykonano,
  backend zostal zrestartowany” i moze zaproponowac ja ponownie. Tryb YOLO i obrazy nie sa zapisywane
- Nowe ustawienie `SESSION_KEEP_DAYS` (domyslnie 7); `0` wylacza zapis i przy starcie usuwa zapisane sesje

### Testy

- Kazdy test dostaje domyslny `DATA_DIR` w katalogu tymczasowym — zaden test nie zapisze nic do `backend/data`

## v0.29.0 (2026-10-09)

### Tryb obserwacji

- `sudo bash scripts/install-server.sh --profile observe` instaluje Pipe tak, zeby niczego nie zmienial na serwerze.
  Pilnuje, wysyla alerty i raporty, odpowiada na pytania, rysuje schematy i dziala jako bramka MCP do odczytu
- W aplikacji (`PIPE_OBSERVE=1`, takze w trybie native): komendy zmieniajace stan, zapis plikow, `/cofnij`, cron,
  `mcp_manage` i `pipe_update apply` sa odrzucane bez pytania o TAK, rowniez w trybie YOLO. Bramka MCP odmawia
  zamiast tworzyc zgode, a zgody nie da sie zatwierdzic. Rutyny, cele, SERVER.md i skille dzialaja jak dotad
- W kontenerze (`backend/docker-compose.observe.yml`): bez `docker.sock` (Docker tylko do odczytu przez
  `tecnativa/docker-socket-proxy` w sieci wewnetrznej), `/root` tylko do odczytu, bez uprawnien jadra poza czytaniem
  plikow i pingiem, `no-new-privileges`
- `--profile full` wraca do pelnego trybu; ponowna instalacja bez `--profile` zostawia biezacy

## v0.28.1 (2026-10-09)

### Poprawki

- `scripts/build_site.py` znowu dziala na Pythonie 3.11 (f-string z backslashem jest dozwolony dopiero od 3.12).
  Przez to od v0.26.1 nie przechodzily testy w CI na 3.11 i nie powstalo wydanie v0.28.0 — pierwszym wydaniem
  z obrazami w GHCR jest v0.28.1

### README

- Nowa sekcja „Dla Claude Code, Codexa i innych agentow”: jak podlaczyc swojego agenta do serwera przez Pipe (MCP)
  zamiast przez SSH, z gotowym poleceniem dla Claude Code
- Instalacja: gotowy obraz z ghcr.io, ostrzezenie o darmowym tierze Gemini i opcja „Bez modelu”;
  w sekcji o bezpieczenstwie link do `SECURITY.md`

## v0.28.0 (2026-10-09)

### Podpisane wydania i gotowe obrazy

- Tag `vX.Y.Z` uruchamia wydanie (`.github/workflows/release.yml`): testy, obrazy `ghcr.io/kvmyk/pipe`
  i `ghcr.io/kvmyk/pipe-telegram` dla amd64 i arm64 z SBOM, podpis cosign bez kluczy i wydanie na GitHubie
  z sekcja changelogu i poleceniem weryfikacji (`scripts/release_notes.py`)
- Instalator pobiera obraz wydania o wersji z pliku `VERSION` zamiast budowac go na serwerze; buduje tylko wtedy,
  gdy obrazu nie ma albo podasz `--build`. `/aktualizuj` po `git pull` tez najpierw pobiera obraz
- Manifesty Kubernetes moga wskazywac gotowe obrazy z GHCR (`deploy/kubernetes/README.md`)

### Zglaszanie podatnosci

- Nowy `SECURITY.md` (po polsku i angielsku): prywatne zgloszenie przez GitHub, co jest, a co nie jest podatnoscia,
  jak sprawdzic podpis obrazu

## v0.27.0 (2026-10-09)

### Pipe bez modelu jezykowego

- Nowa opcja `LLM_PROVIDER=none` (w kreatorze ostatnia pozycja: „Bez modelu”). Pipe pilnuje wtedy serwera bez
  zadnego LLM: czuwanie i alerty, migawki, poranny raport, certyfikaty i strony, audyt, dziennik i `/cofnij`,
  diagramy, webhooki i bramka MCP dzialaja, a z serwera nic nie wychodzi. Rozmowa, rutyny i badanie alertow
  odpowiadaja jasnym komunikatem zamiast bledu polaczenia
- Provider dodany pozniej w `pipe web` albo `/providerzy` wlacza rozmowe bez restartu
- `python3 -m backend.configure --check` i `--models` rozumieja tryb bez modelu, baner przy starcie mowi „bez modelu”

### Prywatnosc przy darmowym Gemini

- Kreator (takze `--from-env`), `pipe web` i CLI ostrzegaja przy wyborze Gemini, ze w darmowym tierze Google moze
  uzywac tresci zapytan (logow, konfiguracji, wynikow komend) do ulepszania swoich uslug, i podpowiadaja platny
  tier albo lokalna Ollame. Lista providerow ma nowe pole `privacy`

### Testy

- Kreator liczy domyslna sciezke `backend/.env` przy wywolaniu, a nie przy imporcie, a kazdy test dostaje wlasny
  plik `.env` — test nie moze juz nadpisac prawdziwej konfiguracji

## v0.26.1 (2026-10-04)

### Dokumentacja na stronie projektu

- Strona projektu (GitHub Pages) ma dokumentacje: polska pod `/docs/`, angielska pod `/en/docs/`. Strony powstaja
  przy budowie z `docs/*.md` i `docs/en/*.md` (`scripts/build_site.py`, wlasny konwerter Markdown bez zaleznosci),
  maja menu wszystkich stron, spis sekcji i przelacznik PL/EN na te sama strone w drugim jezyku
- Nowy katalog `docs/en/`: angielskie wersje szybkiego startu, funkcji, CLI, Telegrama, wdrozenia, bezpieczenstwa,
  backendu i protokolu. `README.en.md` i angielska strona glowna linkuja do nich zamiast do polskich plikow
- Strona glowna (PL i EN) ma w nawigacji i stopce link do dokumentacji; workflow strony rusza takze po zmianie
  `docs/*.md`, `docs/en/*.md` i `VERSION`, a `bump_version.py` podbija wersje rowniez w `docs/en/`
- Test `test_site.py` pilnuje, zeby kazda strona istniala w obu jezykach z ta sama liczba sekcji i zeby
  zbudowana strona nie miala zepsutych linkow ani kotwic

## v0.26.0 (2026-10-04)

### Zalaczniki: pliki, zrzuty ekranu, logi

- Agentowi mozna wyslac plik razem z pytaniem. Telegram: zdjecie albo dokument, podpis to pytanie, album kilku
  zdjec idzie jako jedna wiadomosc. `pipe web`: spinacz, przeciagniecie pliku na okno, wklejenie zrzutu ze schowka,
  miniatury przed wyslaniem. CLI: `/plik <sciezka> [pytanie]` albo `@sciezka` w zwyklej wiadomosci, Tab podpowiada
  sciezki
- Protokol: opcjonalne pole `attachments: [{name, mime, data}]` w zadaniu `message` (stare klienty dzialaja bez
  zmian). Limity: 10 MB na plik, 20 MB i 10 plikow na wiadomosc; `READ_LIMIT` serwera i limit mostu `pipe web`
  podniesione do 32 MiB
- Rodzaj pliku wynika z zawartosci: obraz idzie do modelu jako obraz i zostaje w historii tylko na jedna ture,
  tekst jest wklejany po ukryciu sekretow i przycieciu do 24 tys. znakow, reszta trafia do `DATA_DIR/uploads/`
  (prawa 0600, sprzatane po 7 dniach), a agent dostaje sciezke (w Dockerze takze sciezke na hoscie)
- Model, ktory nie przyjmuje obrazow, nie przemilcza tego: Pipe pisze, ze ten model nie widzi obrazow, i odpowiada
  bez obrazu
- Tresc zalacznika to dane, nie polecenia: YOLO jest wstrzymane do nastepnej wiadomosci, tekst z pliku nie uczy
  VIBE i nie liczy sie jako adres wpisany przez uzytkownika dla `web_fetch`. Viewer moze wysylac obrazy i tekst,
  ale nie pliki do zapisania na serwerze

### Glosowki

- Na Gemini transkrypcja dziala bez `STT_*`: nagranie przepisuje sam model (wejscie audio w Chat Completions).
  Transkrypcja korzysta z providera wybranego w interfejsie, a jej koszt trafia do licznika `/koszt`
- `pipe web` ma przycisk mikrofonu: nagranie do 5 minut, w przegladarce zamieniane na WAV 16 kHz, tekst idzie do
  agenta od razu
- Limit nagrania podniesiony do 10 MB; bot pokazuje blad transkrypcji zamiast wysylac pusta wiadomosc

## v0.25.4 (2026-10-04)

### Strona projektu i README

- Nowa strona projektu na GitHub Pages (`site/`: polska pod `/`, angielska pod `/en/`): czym jest Pipe, zapis
  przykladowej rozmowy z karta potwierdzenia, co agent robi sam, o co pyta i czego nie zrobi nigdy, instalacja
  i uczciwa lista ograniczen. Kroje i kolory jak w `pipe web`, jasny lub ciemny motyw wedlug systemu
- `scripts/build_site.py` sklada strone (site/ + logo, nagrania, kroje IBM Plex), a workflow
  `.github/workflows/pages.yml` publikuje ja po kazdej zmianie. Jednorazowo trzeba wlaczyc w repozytorium
  Settings -> Pages -> Source: GitHub Actions
- README (polskie i angielskie) przepisane prostszym jezykiem i z polskimi znakami: czym jest Pipe, przyklady,
  co potrafi, instalacja, bezpieczenstwo z ograniczeniami. Szczegoly (narzedzia, komendy, architektura) zostaly
  w `docs/`
- `bump_version.py` podbija tez wersje na stronie

## v0.25.3 (2026-10-04)

### Dokumentacja

- Nowy `docs/roadmap.md` z planem nastepnej funkcji: zalaczniki (pliki, zdjecia) i glosowki w Telegramie,
  `pipe web` i CLI -- zakres, stan wyjsciowy i decyzje do potwierdzenia przed startem. `CLAUDE.md` wskazuje na
  ten plan w sekcji *Next up*

## v0.25.2 (2026-10-04)

### Repozytorium

- Angielskie nagranie w `README.en.md` jest w calosci po angielsku: serwer `vps-shop`, domena `shop.example.com`,
  projekt `shop`, plik `shop.conf` (wczesniej polskie nazwy ze scenariusza). Atrapa backendu w `scripts/demo/`
  dobiera nazwy do jezyka nagrania

## v0.25.1 (2026-10-04)

### Wyglad

- `pipe web` startuje domyslnie w ciemnym motywie; jasny zostaje, gdy ktos go wybral przyciskiem (wybor pamieta przegladarka)
- Znak Pipe ma dluzsza nozke -- w logo README, ikonie karty przegladarki i gornym pasku `pipe web`
- Nagrania w README sa w ciemnym motywie i plynniejsze: 25 klatek na sekunde zamiast 12 (`scripts/demo/record.py`
  ustawia motyw jawnie i wypisuje, ile klatek zlapal)

## v0.25.0 (2026-10-04)

### Panel boczny w `pipe web`

- Krawedz panelu ze schematem mozna przeciagnac -- wezej albo szerzej, najwyzej do polowy ekranu (domyslnie pol na
  pol); dwuklik przywraca domyslna szerokosc
- Panel mozna schowac: przyciskiem w pasku zakladek, przeciagnieciem krawedzi do prawej strony okna albo przyciskiem
  w gornym pasku, ktory go tez rozwija. Rozmowa zajmuje wtedy cale okno, tresc w kolumnie posrodku. Komenda albo
  powiadomienie, ktore otwiera zakladke (np. `/alerty`), rozwija panel sam. Szerokosc i stan zostaja w przegladarce
- Obsluga z klawiatury: fokus na krawedzi, strzalki zmieniaja szerokosc, Enter chowa panel

### Poprawki

- Zakladki *Zmiany*, *Skille* i *Alerty* nie migaja juz przy przelaczaniu: listy sa przebudowywane tylko, gdy dane
  sie zmienily, a karty nie powtarzaja animacji wejscia po ponownym pokazaniu zakladki. Rozwiniete podglady skilli
  i roznic nie zwijaja sie przy przelaczaniu
- Wyszukiwanie i czytanie stron swieci na schemacie na elemencie *Internet*, a czytanie logow `/var/log/nginx`
  -- na nginx (wczesniej caly serwer)

### Repozytorium

- README z logo, odznakami i nagraniem `pipe web` (osobne dla polskiej i angielskiej wersji)
- `scripts/demo/`: atrapa backendu z zaplanowanym scenariuszem (prawdziwy interfejs i formaty ramek, bez serwera
  i LLM) oraz `record.py`, ktory nagrywa z niej GIF do README jedna komenda (Playwright + ffmpeg); `--full` dodaje
  przykladowe skille, alerty i dziennik do testow interfejsu
- Wersja w README jest teraz w odznace -- `bump_version.py` podbija ja sam

## v0.24.0 (2026-10-04)

### Internet dla agenta -- bez kluczy API

- Nowe narzedzie `web_search`: agent sam decyduje, kiedy szukac w sieci (nieznany blad, zmiany w nowej wersji,
  CVE, dokumentacja). Zrodla po kolei: DuckDuckGo (strony html i lite), a gdy nie odpowiada -- publiczne API
  Stack Exchange (Server Fault, Stack Overflow), na koncu Wikipedia. Zadnego klucza ani konta, wyniki
  pamietane 15 minut, odstep miedzy zapytaniami do DuckDuckGo
- Nowe narzedzie `web_fetch`: czyta strone. Z pytaniem strone czyta osobne zapytanie do modelu (`WORKER_MODEL`)
  bez zadnych narzedzi, a do agenta trafia tylko odpowiedz -- mniej tokenow i mniejsze ryzyko prompt injection
- Nowe narzedzie `software_info`: `eol` -- koniec wsparcia wersji z endoflife.date (np. "PostgreSQL 16 wspierane
  do 2028-11-09, w cyklu jest 16.15"), `vulns` -- znane podatnosci pakietu w zainstalowanej wersji z OSV.dev,
  najpierw te, ktore naprawia aktualizacja
- `WEB_SEARCH=off` w `backend/.env` zabiera agentowi internet (domyslnie `on`)

### Bezpieczenstwo internetu

- Zapytanie, adres albo argumenty z sekretem sa odrzucane, zanim cokolwiek wyjdzie z serwera
- `web_fetch` czyta bez pytania tylko adresy z wynikow wyszukiwania tej rozmowy albo wpisane przez uzytkownika;
  kazdy inny adres wymaga potwierdzenia (adres moze wynosic dane)
- Tylko publiczny internet: adresy prywatne, loopback, link-local i metadane chmury (`169.254.169.254`) sa
  odrzucane, takze po przekierowaniu
- Tresc z internetu jest oznaczona jako dane, nie polecenia; po jej przeczytaniu tryb YOLO jest wstrzymany do
  nastepnej wiadomosci uzytkownika -- kazda zmiana znow czeka na TAK

## v0.23.0 (2026-10-04)

### Nowy wyglad `pipe web`

- Spokojniejszy interfejs: pelne panele z liniami podzialu zamiast szklanych kart, bez rozmytych plam w tle
  i gradientow. Kolor tylko z funkcja: niebieski -- agent pracuje, bursztyn -- czeka na zgode, zielony / czerwony -- wynik.
  Jasny i ciemny motyw
- Kroj IBM Plex Sans i IBM Plex Mono dolaczony do strony (`clients/webui/static/fonts/`, licencja SIL OFL 1.1) --
  strona nadal nie laduje nic z zewnatrz
- Karta potwierdzenia: komenda w osobnej linii `$ ...` (dokladnie to, co zostanie wykonane), plan bezpiecznika
  jako lista krokow z ikonami (kopia, sprawdzenie przed, weryfikacja po, przywrocenie, cofniecie)
- Os czasu ma naglowek "Aktywnosc agenta" i godzine przy kazdym dzialaniu
- Zakladka *Zmiany*: przetlumaczone statusy wpisow ("wykonano", "cofnieto automatycznie"...), data i narzedzie
  w naglowku karty. Zakladka *Alerty*: przetlumaczone poziomy ("ostrzezenie", "krytyczny")
- Nowa ikona strony i znak Pipe na ekranie powitalnym

### Nowy wyglad CLI

- Naglowek bez ASCII-artu i ramki: znak, wersja, serwer i najczestsze komendy
- Potwierdzenie jako panel: wstep, komenda w osobnej linii jako zwykly tekst (znak w znak), plan bezpiecznika jako tabela
- Kolor tylko z funkcja (bursztyn -- potwierdzenie, zielony / czerwony -- wynik), prompt `›`

## v0.22.1 (2026-10-03)

### Wybor modelu razem z providerem

- Wybor providera prowadzi teraz zawsze do wyboru modelu z listy, ktora zwraca ten provider -- w CLI, `pipe web`
  i na Telegramie. Obecny model jest zaznaczony i domyslny (Enter / "Zostaw ..." niczego nie zmienia),
  fragment nazwy zaweza liste, a jedno trafienie wybiera od razu
- Ten sam krok sluzy do zmiany samego modelu: `/providerzy model` albo wybranie aktywnego providera
- CLI: lista modeli z numerami; `/providerzy groq llama` wybiera model po fragmencie nazwy, a nazwa spoza listy
  providera wymaga potwierdzenia. Gdy listy nie da sie pobrac (np. lokalna Ollama nie odpowiada), nazwe mozna wpisac recznie
- `pipe web`: klikniecie providera w przelaczniku nad polem wpisywania otwiera krok z modelem (wczesniej przelaczalo
  od razu, z poprzednim modelem); Enter zapisuje zaznaczony
- Telegram: po wyborze providera modele przychodza jako przyciski ze stronami (◀ 2/6 ▶), otwarte na stronie
  z obecnym modelem; aktywny provider tez ma przycisk (✓) -- zeby zmienic model

## v0.22.0 (2026-10-03)

### Tryb YOLO (`/yolo`)

- Nowa komenda `/yolo on` / `/yolo off` w CLI, `pipe web` i na Telegramie: zmiany w tej rozmowie wykonuja sie od razu,
  bez pytania o TAK. Domyslnie wylaczony, tylko dla administratora, dotyczy tylko biezacej rozmowy i znika po
  restarcie backendu
- Bezpiecznik dziala jak po TAK: kopia plikow, sprawdzenie przed, weryfikacja po, automatyczne przywrocenie configow
  i wpis w dzienniku -- `/cofnij` cofa takze zmiany z YOLO. Operacje zakazane sa nadal odrzucane, workery nadal tylko czytaja
- Tryb jest widoczny: czerwone `YOLO` w prompcie CLI, pulsujacy znacznik nad polem wpisywania w `pipe web`
  (klikniecie wylacza), komunikat "YOLO — wykonuje bez pytania: ..." przy kazdej operacji, dopisek `[yolo]` przy
  interfejsie w audit logu i dzienniku
- Agent dostaje w prompcie instrukcje na ten tryb: ostroznie, najmniejsza zmiana, bez nieodwracalnych operacji
  bez wyraznej prosby

### Providerzy LLM z komendy (`/providerzy`, `/providers`)

- CLI: lista providerow, wybor numerem, klucz API wpisywany bez echa, model z listy providera;
  `/providerzy groq [model]` przelacza od razu, `/providerzy zapomnij <id>` usuwa klucz dodany z interfejsu
- `pipe web`: komenda otwiera ekran wyboru providera, `/providerzy groq` przelacza od razu (stare `/provider` dziala dalej)
- Telegram: lista z przyciskami przelaczenia i `/providerzy groq [model]`. Bot celowo nie przyjmuje kluczy API
  (zostalyby w historii czatu) -- dodasz je w `pipe web` albo w CLI

### Projekt open source

- Pipe ma licencje MIT (plik `LICENSE`) -- wczesniej repozytorium nie mialo zadnej licencji
- Instalator (`scripts/install-server.sh`), cloud-init i dokumentacja klonowaly repozytorium spod nieistniejacego
  adresu `github.com/user/pipe` -- teraz `github.com/Kvmyk/pipe`. Instalacja na swiezym serwerze bez repo znowu dziala
- Usunieto nieaktualne `TODO.md` (analiza z czasow v0.3); `AGENTS.md` odsyla teraz do `CLAUDE.md`

## v0.21.5 (2026-10-02)

### Wiadomosci od Pipe w CLI

- Wynik `/aktualizuj` zleconej z CLI (i kazde przypomnienie ustawione z CLI) dociera teraz takze do CLI. Wczesniej,
  gdy dzialal bot Telegrama albo otwarta byla strona `pipe web`, wiadomosc szla tylko do nich i znikala, zanim
  CLI zdazylo ja odebrac
- Takie przypomnienie idzie do Telegrama i weba jak dotad, a dodatkowo czeka na odbior w CLI; `/przypomnienia`
  pokazuje je jako "wyslane, czeka na odbior w CLI"
- CLI pokazuje wiadomosci od Pipe samo, nad promptem, gdy czeka na wpis (sprawdza co 10 s) -- nie trzeba juz nic
  wpisywac, zeby je zobaczyc. Wymaga `prompt_toolkit`; bez niego dziala jak dotad (po polaczeniu i po kazdej wymianie)
- Poprawka dotyczy tez backendu: zadziala po aktualizacji serwera do tej wersji

## v0.21.4 (2026-10-02)

### CLI

- Lista podpowiedzi po `/` nie pokazuje juz `/jezyk` dwa razy -- wpis byl zdublowany w tabeli komend
  (sama komenda dzialala poprawnie)

## v0.21.3 (2026-10-02)

### CLI

- Po `/aktualizuj` nie trzeba juz uruchamiac CLI od nowa. Gdy backend znika (przebudowa kontenera po aktualizacji,
  zerwane polaczenie), CLI samo laczy sie ponownie -- probuje co 2 s przez maksymalnie 3 minuty -- i ponawia
  wiadomosc albo komende. Wczesniej po jednej nieudanej probie konczylo prace
- Gdy backend nie wroci w tym czasie, CLI pokazuje blad, ale zostaje otwarte; nastepna wiadomosc probuje znowu
- Zadanie przerwane w polowie odpowiedzi nie jest powtarzane (agent mogl juz cos wykonac) -- CLI pokazuje to,
  co doszlo, i ostrzezenie
- Ograniczenie bez zmian: po restarcie backendu agent nie pamieta wczesniejszej rozmowy (sesje sa w pamieci procesu)

## v0.21.2 (2026-10-02)

### CLI

- Baner powitalny jest w jezyku agenta: pojawia sie dopiero po polaczeniu, wiec uwzglednia jezyk wybrany komenda
  `/jezyk` (wczesniej zawsze byl w jezyku startowym CLI). `--lang` dalej ma pierwszenstwo
- Gdy brakuje `prompt_toolkit`, CLI wyraznie mowi, ze podpowiedzi komend sa wylaczone, i podaje gotowe polecenie
  instalacji dla tego Pythona, z ktorego dziala -- wazne, gdy `pipe` startuje z wlasnego venv, do ktorego
  pakiety systemowe nie siegaja

## v0.21.1 (2026-10-02)

### CLI

- Haslo SSH nie trafia juz do rozmowy z agentem. Gdy lokalny port tunelu (domyslnie 7379) byl zajety -- przez
  drugie otwarte CLI albo tunel pozostaly po wczesniejszym uruchomieniu -- CLI bralo cudzy tunel za swoj
  i zaczynalo rozmowe, zanim jego wlasne `ssh` skonczylo logowanie; wpisywane haslo ladowalo wtedy jako wiadomosc
  do agenta. Teraz CLI sprawdza port przed zestawieniem tunelu (takze `kubectl port-forward`) i przy zajetym
  bierze inny wolny, o czym informuje jedna linia
- Jesli zdarzylo Ci sie wpisac haslo w rozmowie z agentem -- zmien je

## v0.21.0 (2026-10-02)

### Zmiana jezyka w trakcie pracy

- `/jezyk en` i `/jezyk pl` (po angielsku `/language`) w CLI, w `pipe web` i na Telegramie -- przelacza jezyk
  calego Pipe bez restartu: instrukcje agenta (system prompt, opisy narzedzi), raporty i komunikaty.
  Samo `/jezyk` pokazuje obecny jezyk
- Wybor jest wspolny dla wszystkich kanalow: po zmianie z jednego bot Telegrama sam przelacza komunikaty i menu `/`,
  a otwarta strona `pipe web` proponuje odswiezenie. CLI przejmuje wybrany jezyk przy polaczeniu (chyba ze podano `--lang`)
- `pipe web`: przycisk PL/EN w naglowku; strona odswieza sie w nowym jezyku, a agent pamieta rozmowe
- Wybor zapisuje sie w `DATA_DIR/language.json` i zostaje po restarcie; reczna zmiana `PIPE_LANG` w `.env` znow ma
  pierwszenstwo. Nietkniete skille wbudowane o tej samej nazwie w obu jezykach sa podmieniane na nowa wersje
- Jezyk zmienia tylko administrator; rola viewer widzi obecny jezyk
- Protokol: komenda `language`, zdarzenie `language` dla subskrybentow, pola `lang` i `lang_chosen` w `subscribed`

### CLI

- Podpowiedzi komend: po wpisaniu `/` pod promptem pojawia sie lista komend i skilli z opisami, zawezana w trakcie
  pisania (pasuje nazwa polska, angielska i alias); `Tab` wstawia pozycje, strzalka w gore przywoluje wczesniejsze wpisy
- Wymaga `prompt_toolkit` (doszedl do `clients/cli/requirements.txt`): po aktualizacji uruchom
  `pip install -r clients/cli/requirements.txt`. Bez niego CLI dziala jak dotad, bez podpowiedzi

## v0.20.1 (2026-10-02)

### `pipe web`

- Logotypy providerow zamiast monogramow: na ekranie wyboru, w przelaczniku nad polem rozmowy i w jego menu.
  Sluza wylacznie wskazaniu, z czyimi modelami laczy sie agent; Pipe nie jest powiazany z tymi firmami
  (`clients/webui/static/providers/NOTICE.md`). Wlasni providerzy dalej maja monogram
- Strona nie gubi juz zdarzen, ktore przyszly, gdy nie byla podlaczona (np. wiadomosc o wyniku aktualizacji
  po restarcie backendu): przy starcie i po kazdym ponownym polaczeniu dobiera ostatnie zdarzenia z backendu

## v0.20.0 (2026-10-02)

### Komenda `/aktualizuj`

- `/aktualizuj` (po angielsku `/update`) w CLI, na Telegramie i w `pipe web` -- aktualizuje Pipe na serwerze jednym
  poleceniem: pyta o potwierdzenie, pobiera zmiany i przebudowuje kontenery (mechanizm z v0.19.0)
- `/aktualizuj sprawdz` (`/update check`) tylko pokazuje wersje i to, czy jest nowsza -- niczego nie zmienia

### `pipe web`

- Znaczki providerow na ekranie wyboru, w przelaczniku nad polem rozmowy i w jego menu -- latwiej odroznic
  providerow na pierwszy rzut oka
- To wlasne monogramy Pipe (litera na kolorowym tle), a nie logotypy firm: logotypy providerow sa znakami
  towarowymi i ich uzycie wymaga zgody wlascicieli, wiec ich nie kopiujemy. Nazwy pozostaja zwyklym tekstem

## v0.19.0 (2026-10-02)

Pipe potrafi zaktualizowac sam siebie. Ten jeden raz aktualizacje trzeba jeszcze zrobic recznie na serwerze
(`git pull` + `docker compose up -d --build`) -- nowy obraz zawiera narzedzie, ktorego stary nie ma.

### Aktualizacja z rozmowy

- **"Zaktualizuj sie"** -- nowe narzedzie agenta `pipe_update`. `check` pokazuje wersje, commit i to, czy na zdalnym
  repozytorium sa nowsze zmiany; `apply` po potwierdzeniu pobiera je i przebudowuje uslugi Pipe
- Przebudowe wykonuje **osobny kontener pomocniczy** -- agent nie moze wymienic kontenera, w ktorym sam dziala.
  Kontener robi `git pull --ff-only` i `docker compose up -d --build` tylko dla uslug, ktore juz dzialaly
- Po restarcie przychodzi wiadomosc z wynikiem ("Pipe zaktualizowany: 0.18.0 -> 0.19.0") do tego, kto zlecil
  aktualizacje -- takze na Telegram. Gdy pobranie zmian albo budowanie sie nie uda, stara wersja dziala dalej,
  a wiadomosc zawiera koniec logu
- Dziala w trybie Docker. W trybie native i w Kubernetesie agent podaje kroki do wykonania recznie

### Bezpieczenstwo

- Narzedzie nie przyjmuje adresu, galezi ani komendy -- kod przychodzi wylacznie z `origin` repozytorium, w ktorym
  Pipe jest zainstalowany. Wymaga potwierdzenia; rola viewer nie moze go uzyc
- `docker compose` nadal nie jest dostepny dla agenta jako komenda: plik compose w obrazie lezy poza katalogiem
  wtyczek i sluzy tylko aktualizacji

## v0.18.0 (2026-10-02)

Wybor providera LLM w przegladarce i przelaczanie miedzy providerami w trakcie rozmowy. Po aktualizacji zaktualizuj
tez backend na serwerze (`git pull` + `docker compose up -d --build`) -- nowe komendy dzialaja po jego stronie.

### `pipe web`

- **Ekran wyboru providera** przy pierwszym wejsciu: lista providerow, pole na klucz API, sprawdzenie klucza
  u providera i wybor modelu z jego aktualnej listy. Mozna go pominac i zostac przy tym z instalacji
- **Przelacznik w rozmowie**: nad polem wiadomosci widac, kto odpowiada (provider i model); klikniecie rozwija
  liste providerow, do ktorych jest klucz, i przelacza jednym ruchem -- bez restartu, rozmowa trwa dalej
- Komenda `/provider` otwiera ekran ponownie: kolejny klucz, zmiana modelu, usuniecie klucza dodanego z interfejsu
- Provider jest gotowy, gdy ma klucz: dodany z interfejsu, `LLM_API_KEY` albo zmienna presetu w `backend/.env`
  (`OPENAI_API_KEY`, `GROQ_API_KEY`...) -- kilka kluczy w `.env` od razu daje kilka pozycji w przelaczniku

### Backend

- `core/llm.py`: klucze i wybor w `DATA_DIR/llm_keys.json` (0600); `backend/.env` zostaje providerem bazowym.
  Wybor dotyczy calego agenta -- takze Telegrama, CLI, rutyn i workerow
- Komendy `providers`, `provider_models`, `provider_set`, `provider_forget` (zmiany tylko dla administratora).
  Klucz nigdy nie wraca do klienta ani do logow; nowy klucz jest zapisywany dopiero, gdy provider go przyjmie
- Po przelaczeniu `WORKER_MODEL` i `LLM_REASONING_EFFORT` z `.env` nie sa wysylane innemu providerowi, a koszt jest
  liczony w samych tokenach (ceny z `.env` dotycza providera bazowego)
- Historia rozmowy sprzed przelaczenia jest sprowadzana do pol standardu Chat Completions -- pola wlasne jednego
  providera (np. podpisy rozumowania) nie trafiaja do innego
- `llm_keys.json` jest plikiem wrazliwym: odczyt przez agenta wymaga potwierdzenia

## v0.17.0 (2026-10-01)

Interfejs w przegladarce ze schematem serwera na zywo. Aktualizacja bez recznych krokow; na serwerze nic sie nie
zmienia w konfiguracji -- nowa komenda dziala po stronie klienta.

### `pipe web`

- Rozmowa z agentem w przegladarce: `pipe web` zestawia tunel jak CLI i otwiera strone na `127.0.0.1:7400`.
  Bez instalowania i budowania czegokolwiek; token backendu zostaje w procesie `pipe web`
- **Schemat na zywo** obok rozmowy, generowany automatycznie: serwery -> wnetrze serwera -> projekt compose.
  Element, na ktorym agent pracuje, jest podswietlony; dzialanie czekajace na zgode jest oznaczone osobno
- **Sledzenie agenta**: widok sam przechodzi tam, gdzie agent pracuje; wlasny ruch na schemacie wylacza sledzenie,
  przycisk wlacza je z powrotem
- Os czasu dzialan z krokami bezpiecznika i workerow; szczegoly elementu po kliknieciu
- **Zmiany**: roznica pliku w karcie potwierdzenia, a po wykonaniu roznica "przed -> po" w rozmowie i w zakladce
  *Zmiany*, z przyciskiem cofniecia
- Alerty, przypomnienia i raporty przychodza na strone na zywo; aktywne alerty maja przycisk *Zbadaj*
- **Komendy ze slashem**: po wpisaniu `/` rozwija sie lista wszystkich komend i skilli z opisami (strzalki,
  Enter, Tab, Esc); dzialaja te same komendy co w CLI i na Telegramie, takze angielskie aliasy
- Zakladka **Skille**: lista z opisami, podglad tresci i uruchamianie jednym kliknieciem
- Jasny motyw w blekitach (domyslny) i ciemny; polski i angielski (`--lang`)

### Backend

- Komenda `graph` -- schemat infrastruktury jako dane (wezly, polaczenia, trzy poziomy)
- Zdarzenia `activity` dla klientow webowych: co agent robi i na ktorym elemencie schematu
- Komenda `journal_changes` -- roznica "przed -> teraz" dla wpisu dziennika, z redakcja sekretow
- Komenda `skill` -- tresc jednego skilla (podglad)

## v0.16.2 (2026-10-01)

Czytelniejsze wiadomosci na Telegramie. Aktualizacja: przebuduj kontener bota (`docker compose up -d --build`).

### Telegram

- Raporty rutyn, badania alertow i zadan jednorazowych przychodza jako zwykla wiadomosc zamiast bloku kodu:
  status w naglowku, pogrubione sekcje, listy, komendy doslownie; tagi HTML z raportu sa pokazywane jako tekst
- `/zmiany`, `/zdrowie`, `/koszt`, `/dziennik`, `/przypomnienia`, `/incydenty` i wynik `/cofnij` to naglowki i listy;
  identyfikatory wpisow mozna skopiowac dotknieciem
- Blok kodu zostal tylko dla logu audytu (`/historia`), roznic plikow i wynikow komend

## v0.16.1 (2026-10-01)

Poprawki przypomnien. W Dockerze po `git pull` zrob `docker compose up -d --build` (zmienil sie `docker-compose.yml`).

### Poprawki

- Przypomnienie, ktorego bot nie mogl odebrac, konczylo sie cisza, a agent twierdzil, ze wiadomosc wyslal. Teraz
  agent dostaje informacje, ze bot nie odbiera powiadomien, i mowi o tym od razu; `/przypomnienia` i `/alerty`
  pokazuja ostrzezenie z powodem (np. odrzucony token, brak polaczenia z backendem)
- `TELEGRAM_ALERTS=0` wylaczalo cala subskrypcje zdarzen, wiec nie dochodzily tez przypomnienia ani prosby o zgode
  dla agentow MCP. Teraz wycisza tylko alerty czuwania i raporty
- Bot nie logowal nieudanych prob polaczenia z backendem -- w logu jest teraz `Kanal zdarzen nieaktywny: <powod>`
- Kontenery chodzily w UTC niezaleznie od strefy hosta: *"przypomnij o 9"*, rutyny i poranny raport przychodzily
  o innej godzinie niz na zegarze serwera. Kontenery biora teraz strefe hosta; `TZ=Europe/Warsaw` w `backend/.env`
  ustawia wlasna. Komunikaty o przypomnieniach podaja strefe

## v0.16.0 (2026-10-01)

Przypomnienia: agent moze odezwac sie pozniej, gdy go o to poprosisz. Aktualizacja bez recznych krokow.

### Przypomnienia

- *"napisz do mnie za 10 sekund"*, *"przypomnij jutro o 9 o odnowieniu domeny"* -- wiadomosc przychodzi sama
  o czasie, z dokladnoscia do sekundy, bez udzialu LLM
- *"za godzine sprawdz, czy backup sie skonczyl"* -- zadanie jednorazowe: o czasie worker (tylko odczyty) sprawdza
  i przysyla raport; wymaga potwierdzenia, tak jak rutyna
- Przypomnienie ustawione na Telegramie wraca do osoby, ktora je ustawila; ustawione w CLI -- do administratorow
  na Telegramie
- Przetrwa restart backendu (`backend/data/reminders.json`); gdy bot nie jest podlaczony, czeka i dociera po
  polaczeniu. CLI pokazuje zalegle przypomnienia po polaczeniu i przy najblizszej wiadomosci
- Nowe narzedzie `reminder`, komenda `/przypomnienia` (`/reminders`) z anulowaniem: `/przypomnienia anuluj <id>`

### Poprawki

- Agent obiecywal *"odezwe sie za 10 sekund"*, choc nie mial jak tego zrobic. Prompt zabrania teraz obietnic
  na przyszlosc bez zaplanowania ich narzedziem (`reminder` albo `routine_manage`)

## v0.15.0 (2026-10-01)

Pipe po angielsku: jeden przelacznik `PIPE_LANG=en` zmienia jezyk calego produktu. Domyslnie zostaje polski --
aktualizacja bez recznych krokow.

### Jezyk angielski

- `PIPE_LANG=pl|en` w `backend/.env`: system prompt, opisy narzedzi, prompty workerow i rutyn, pytania
  o potwierdzenie z planem bezpiecznika, wyniki narzedzi, alerty czuwania, poranny raport, `/zmiany`, `/zdrowie`,
  `/audyt` z poprawkami, powitanie, pamiec incydentow, dziennik i `/cofnij`, koszty, webhooki, narzedzia serwera MCP
- Kreator `python3 -m backend.configure` pyta o jezyk jako pierwszy i sam sie na niego przelacza;
  `--from-env` przyjmuje `PIPE_LANG`; `scripts/install-server.sh --lang en` (instalator przepisuje jezyk takze
  do `.env` bota Telegrama)
- CLI: `pipe --lang en` albo zmienna `PIPE_LANG`; bot Telegrama: `PIPE_LANG` w `clients/telegram/.env` -- menu `/`,
  przyciski (YES/NO, Investigate, Undo, Approve) i etykiety po angielsku
- Angielskie nazwy komend w obu klientach (`/report`, `/changes`, `/chart`, `/health`, `/audit`, `/map`,
  `/directory`, `/skills`, `/alerts`, `/incidents`, `/routines`, `/targets`, `/journal`, `/undo`, `/approvals`,
  `/cost`, `/history`, `/help`) -- dzialaja obok polskich, niezaleznie od jezyka
- Angielskie wersje siedmiu wbudowanych skilli (`update-container`, `harden-ssh`, `free-disk-space` i cztery
  o tych samych nazwach)
- `README.en.md`

### Poprawki

- Znaczniki protokolu (`[POTWIERDZ]`, `[BLAD]`, `[ODMOWA]`, `[PAMIEC]`, `[ZREDAGOWANO: ...]`) sa niezalezne od
  jezyka; serwer rozpoznaje pytanie o potwierdzenie takze po angielskiej frazie
- Komenda `/incydenty` byla opisana w dokumentacji, ale klienci jej nie obslugiwali -- dziala w CLI i na Telegramie
- CLI po ponownym polaczeniu pisalo po angielsku (`Reconnected.`) takze w polskim interfejsie

## v0.14.0 (2026-09-29)

MCP w obie strony: Pipe jako bezpieczna brama do serwera dla innych agentow AI i Pipe korzystajacy z cudzych
serwerow MCP. Aktualizacja bez recznych krokow; nowe porty i serwery MCP sa domyslnie wylaczone.

### Pipe jako serwer MCP

- Claude Code, Cursor i inni agenci dostaja narzedzia Pipe: `run_command` (przez klasyfikator), `read_file`
  (bez plikow z sekretami), stan, zmiany, mapa, zdrowie, audyt, dziennik i `ask_pipe`
- Zmiana od zewnetrznego agenta czeka na **zgode** administratora -- Telegram (przyciski) albo `/zgody` w CLI --
  i po niej idzie przez bezpiecznik, z kopia i `/cofnij`
- Most stdio `pipe --mcp --host root@serwer` (tunel SSH, tokeny Pipe) i endpoint Streamable HTTP (`MCP_PORT`)
- Protokol 2026-07-28 (`server/discover`, wersja w `_meta`, naglowki `Mcp-Method`/`Mcp-Name`) i starsze wersje
  z `initialize`

### Pipe jako klient MCP

- Narzedzia serwerow MCP (stdio i HTTP) jako narzedzia agenta: `mcp__<serwer>__<narzedzie>`; konfiguracja zdaniem
  albo w `backend/data/mcp.json`
- Kazde wywolanie z potwierdzeniem, chyba ze narzedzie jest na liscie `autoApprove` albo zaufane jako tylko-odczyt;
  dodanie serwera zawsze z potwierdzeniem; podproces nie dostaje klucza LLM ani tokenow Pipe
- Nowe narzedzie `mcp_manage`, komenda `/mcp`

## v0.13.0 (2026-09-29)

Pamiec incydentow, alerty z zewnatrz, wiadomosci glosowe i role. Aktualizacja w Dockerze: `docker compose up -d`
przebuduje kontener z nowym `env_file` -- bez innych recznych krokow.

### Pamiec incydentow

- Rozwiazany alert zostaje w pamieci: ustalenia z *Zbadaj* i zmiany z dziennika wykonane w czasie problemu
- Powtorny alert przychodzi z linia *Poprzednio: ...*, a *Zbadaj* zaczyna od sprawdzonej przyczyny i naprawy
- `/incydenty` i `server_history operation=incidents`

### Alerty z zewnatrz

- Webhooki (`WEBHOOK_PORT`, `WEBHOOK_TOKEN`): Alertmanager, Grafana, Uptime Kuma, GitHub (nieudany workflow,
  wdrozenie) i format ogolny. Alert na Telegram z *Zbadaj*, znika po `resolved`
- `WEBHOOK_INVESTIGATE=1` -- worker od razu bada alert na serwerze (tylko odczyty) i przysyla raport

### Glos i role

- Wiadomosci glosowe na Telegramie: transkrypcja (Whisper przez API zgodne z OpenAI; openai/groq bez konfiguracji,
  inni -- `STT_*`) i odpowiedz jak na tekst
- Rola viewer: `TELEGRAM_VIEWER_IDS` + `AGENT_VIEWER_TOKEN` oraz tokeny klientow `python3 -m backend.tokens`
  (admin/viewer, odwolywalne). Viewer diagnozuje i czyta, nie zatwierdza zmian -- egzekwuje to backend
- Sesja jest przypieta do tokenu, ktory ja zalozyl

### Poprawki

- Docker: caly `backend/.env` trafia do kontenera (`env_file`) -- wczesniej czesc ustawien (np. progi, raport,
  ceny, `WORKER_TIMEOUT`) byla pomijana, bo nie bylo ich na liscie `environment`
- Token z niestandardowymi znakami nie przerywa juz polaczenia wyjatkiem (porownanie na bajtach)

## v0.12.0 (2026-09-29)

Pipe pilnuje bezpieczenstwa serwera, wita sie po instalacji i przychodzi z gotowymi przepisami.
Aktualizacja bez recznych krokow -- skille wbudowane instaluja sie same przy starcie.

### Audyt bezpieczenstwa

- `/audyt` -- ocena 0-100 bez LLM: logowanie SSH haslem i rootem, zapora, bazy danych i API Dockera
  wystawione na swiat (z poprawka w compose, bo Docker omija ufw), kontenery `privileged` i z `docker.sock`,
  konta z uid 0 i bez hasla, automatyczne aktualizacje, fail2ban, pliki `.env` czytelne dla wszystkich,
  synchronizacja czasu, swap, certyfikaty
- Kazdy punkt ma gotowa komende; *"napraw 1"* przechodzi przez potwierdzenie z bezpiecznikiem. Wylaczenie hasel
  SSH trafia do `sshd_config.d/00-pipe-*.conf` i nie jest proponowane bez klucza w `authorized_keys`
- Nowe narzedzie agenta `security_audit`

### Straznik i logowania SSH

- Co 2 minuty: nowe konta (uid 0 = krytyczny), nowe klucze SSH, nowe programy SUID, zmiany sudoers, sshd, PAM
  i `ld.so.preload` -- alert, chyba ze zmiane zrobil Pipe (dziennik)
- Log SSH: seria nieudanych logowan (`WATCH_SSH_FAILURES`), udane logowanie haslem z adresu, ktory zgadywal
  hasla, logowanie z nowego adresu (`WATCH_SSH_LOGINS`)
- Migawki zapisuja programy SUID/SGID

### Pierwsze 5 minut i skille

- Powitanie po instalacji na Telegramie (i w CLI przy pierwszym polaczeniu): mapa, ocena bezpieczenstwa,
  trzy najwazniejsze poprawki i to, czego Pipe pilnuje
- Wbudowane skille: `nginx-vhost`, `swap`, `fail2ban-ssh`, `backup-postgres`, `aktualizuj-kontener`,
  `utwardz-ssh`, `wolne-miejsce` -- edytowalne; aktualizacja nie nadpisuje zmian uzytkownika

## v0.11.0 (2026-09-29)

Zmiany z bezpiecznikiem: kazda zatwierdzona operacja ma kopie, sprawdzenie przed, weryfikacje po i da sie ja
cofnac. Aktualizacja bez recznych krokow.

### Bezpiecznik

- Potwierdzenie pokazuje plan: co zostanie skopiowane, co Pipe sprawdzi przed zmiana i co zweryfikuje po niej
- Przed przeladowaniem uslugi -- walidacja konfiguracji (`nginx -t`, `sshd -t`, `caddy validate`,
  `apachectl configtest`, `haproxy -c`, `docker compose config -q`, takze `docker exec <nginx> nginx -t`).
  Nieudane sprawdzenie = operacja nie jest wykonywana
- Po zmianie: usluga aktywna, kontener dziala i nie jest unhealthy, projekt compose wstal, zmieniony plik
  konfiguracji przechodzi walidacje, a strony, ktore odpowiadaly przed zmiana, odpowiadaja po niej
- Nieudana weryfikacja zmiany plikow konfiguracji przywraca kopie automatycznie i przeladowuje usluge ponownie
  (`SAFE_AUTO_ROLLBACK=1`)

### Dziennik zmian i `/cofnij`

- Kopie plikow (`sed -i`, `tee`, `>`, `cp`, `mv`, `rm`, `chmod`, `write_file`), HEAD repozytorium przy operacjach
  gita, crontab i pliki pamieci Pipe (cele, rutyny, skille, SERVER.md) trafiaja do dziennika przed zmiana
- `/dziennik` i `/cofnij [id]` w obu klientach -- podglad roznic i komend odwrotnych (`docker start`,
  `systemctl enable`, `git reset --keep`, `docker compose up -d`), potem TAK. Dziala bez LLM
- Agent cofa zmiany sam, gdy o to poprosisz (nowe narzedzie `journal`); cofniecie tez mozna cofnac

## v0.10.0 (2026-09-29)

Pipe pamieta, jak serwer sie zmienia, i sam wie, co sprawdzac. Aktualizacja bez recznych krokow -- nowe
funkcje dzialaja od razu z domyslnymi ustawieniami.

### Co sie zmienilo -- wehikul czasu serwera

- Co godzine Pipe zapisuje migawke hosta: pakiety, kontenery (z identyfikatorem obrazu -- widac nowa wersje
  pod tym samym tagiem), porty, uslugi systemd, cron, konta, klucze SSH (sam odcisk), konfiguracje sshd,
  sudoers, nginx, Caddy i pliki compose. Zapis tylko przy zmianie
- `/zmiany [24h|3d]` pokazuje zmiany z przedzialem czasu (*miedzy 03:00 a 04:00*); zmiany kont, kluczy SSH,
  sudoers i sshd sa oznaczone `[BEZPIECZENSTWO]`
- Agent zaczyna diagnoze awarii od historii zmian, a przycisk *Zbadaj* przy alercie dolacza zmiany
  z ostatniej doby

### Wykresy i monitoring bez konfiguracji

- Historia pomiarow (load, RAM, swap, dyski) i `/wykres [load|ram|dysk] [24h|7d]` -- obraz z progiem alertu;
  agent rysuje wykres sam, gdy pytasz o trend
- Certyfikaty TLS, odpowiedz HTTPS i DNS domen znalezionych w nginx, Caddy i etykietach Traefika oraz swiezosc
  backupow z wpisow `[backup]` w DIRECTORY -- sprawdzane co godzine, z alertami na Telegram. `/zdrowie`
  pokazuje wszystko naraz
- Nowe narzedzie agenta `server_history` (changes, chart, checks)

### Poranny raport i koszty

- Codziennie o 7:00 (`DIGEST_TIME`) raport na Telegram: stan, alerty, zmiany od wczoraj, certyfikaty, backupy,
  aktualizacje, wymagany restart, rutyny i zuzycie LLM, z wykresem obciazenia. Bez LLM. `/raport` na zadanie
- `/koszt` -- tokeny dzis, w ostatnich dniach i w miesiacu, z podzialem na uzytkownikow, workery, rutyny i VIBE;
  koszt w USD po podaniu `LLM_PRICE_IN` / `LLM_PRICE_OUT`. Dzienny limit: `DAILY_TOKEN_LIMIT` / `DAILY_COST_LIMIT`

### Poprawki

- CI: samo `pytest` (bez `python -m`) znajduje pakiet `backend` -- dodany `pytest.ini`

## v0.9.2 (2026-09-29)

Ciagla integracja w GitHub Actions. Zachowanie agenta sie nie zmienilo -- aktualizacja bez recznych krokow.

### CI

- Kazdy push na `main` i kazdy pull request uruchamia testy (`pytest backend/tests/`) na Pythonie 3.11
  i 3.13, razem z testami bota Telegrama
- Obrazy backendu i bota Telegrama sa budowane przy kazdej zmianie (bez wypychania do rejestru), wiec
  zepsuty `Dockerfile` wychodzi przed wdrozeniem
- Manifesty Kubernetesa (base i wszystkie overlaye) sa renderowane przez `kubectl kustomize`, a skrypty
  instalacyjne sprawdzane `bash -n`

## v0.9.1 (2026-09-28)

Poprawki z przegladu kodu v0.9.0 -- glownie obejscia klasyfikatora komend. Aktualizacja bez recznych krokow.

### Bezpieczenstwo

- Pliki z sekretami wymagaja potwierdzenia takze wtedy, gdy sciezka jest zamaskowana cudzyslowami albo
  backslashem (`/etc/sha""dow`, `.e\nv`), podana wzglednie (`cat .env`) albo jako `/proc/self/environ`
  i `/proc/*/environ`. Rozwiniecia `${...}` i `$'...'` licza sie jako dynamiczne konstrukcje
- `curl` przez obcy proxy (`-x`, `--proxy`, `--socks5`), z `--resolve`/`--connect-to` albo na adres
  zapisany jedna liczba (`http://134744072/`) wymaga potwierdzenia; kazda metoda `-X...` inna niz GET tez
- Zrzut sekretow Kubernetesa z listy zasobow (`kubectl get cm,secret -o yaml`) i przez `--template`
  wymaga potwierdzenia
- `docker restart` i `docker start` nie sa juz traktowane jak odczyt -- workery i rutyny ich nie wykonuja,
  a w rozmowie wymagaja TAK
- `read_file` pyta o zgode przy tych samych plikach z sekretami co `cat` (`.env`, klucze, `*.pem`)
- Potwierdzenie nowej rutyny pokazuje cala tresc zadania, a nie pierwsze 160 znakow

### Poprawki

- Tryb docker: symlinki hosta (np. `sites-enabled` nginx, `/etc/letsencrypt/live`) sa rozwiazywane
  wzgledem korzenia hosta -- `read_file`, `write_file` i `change_directory` przestaly ich odrzucac
  jako "poza workspace"
- Worker przerwany limitem czasu nie psuje juz kolejnego zadania o tej samej nazwie (historia jest
  domykana i przycinana)
- Czuwanie: pierwszy publiczny port na hoscie bez publicznych portow jest zglaszany; nieudany odczyt
  gniazd nie kasuje punktu odniesienia

## v0.9.0 (2026-09-28)

Przebudowa: Pipe z agenta "na pytanie" stal sie agentem operacyjnym, ktory sam pilnuje serwera,
widzi jego architekture i zarzadza wieloma maszynami. Aktualizacja nie wymaga recznych krokow:
`git pull` i ponowne uruchomienie `scripts/install-server.sh` (albo `docker compose up -d --build`).

### Diagramy

- Nowe narzedzie `diagram` i komenda `/mapa`: mapa infrastruktury hosta (domeny -> reverse proxy nginx/Caddy/Traefik -> kontenery -> bazy, projekty compose, porty wystawione na swiat, uslugi systemd, aplikacje z Kubernetesa, zdalne cele). `/mapa` dziala bez LLM
- Agent rysuje tez wlasne diagramy Mermaid (przeplywy, procedury). Telegram dostaje zdjecie, CLI plik PNG w `~/.pipe/diagrams` i podglad ASCII w terminalu
- Renderowanie offline przez mermaidx (bez przegladarki, Node.js i zewnetrznych uslug); blad skladni wraca do modelu

### Pamiec agenta

- DIRECTORY: mapa repozytoriow, katalogow aplikacji, projektow compose, konfiguracji i backupow z opisami, w prompcie kazdej rozmowy. Skan sam znajduje repozytoria git (bez tokenow w adresach) i projekty compose. Komenda `/katalogi`
- VIBE: agent co kilka wiadomosci w tle uczy sie stylu rozmowy uzytkownika (osobna notatka na uzytkownika). `/vibe` pokazuje, `/vibe reset` czysci

### Workery, cele, rutyny i czuwanie

- Zdalne cele (`target_manage`, `remote_exec`): serwery SSH, kontenery Docker, klastry i pody Kubernetes -- bez instalowania czegokolwiek po drugiej stronie. Komenda `/cele`
- Workery (`delegate`): pod-agenci wysylani rownolegle na cele; wykonuja tylko odczyty, zmiany zwracaja jako propozycje do zatwierdzenia. Postep widac na zywo w obu klientach
- Rutyny (`routine_manage`): zadania wedlug harmonogramu cron z raportem na Telegram. Komenda `/rutyny`
- Czuwanie: co 2 minuty sprawdzenia bez LLM (dyski, RAM, obciazenie, kontenery w petli restartow albo unhealthy, zatrzymane kontenery, nowe publiczne porty). Alerty przychodza na Telegram z przyciskiem "Zbadaj". Komenda `/alerty`

### Wdrozenie

- Trzy tryby dzialania (`PIPE_RUNTIME`): docker, native (systemd, bez Dockera) i kubernetes; wykrywane automatycznie
- `scripts/install-server.sh`: instalacja jedna komenda (apt, dnf, apk, pacman, zypper), tez bez pytan (`-y`, `configure --from-env`)
- Manifesty Kubernetes (kustomize): baza tylko do odczytu, bez sekretow, i nakladki operator, telegram, host-agent. CLI laczy sie przez `pipe --kube <namespace>`
- cloud-init dla kazdego dostawcy chmury (`deploy/cloud-init/user-data.yaml`), obraz Dockera dla amd64 i arm64

### Bezpieczenstwo

- Klasyfikator komend przepisany: dopasowanie tokenowe (`ss` nie przepuszcza juz `ssh`, `ps` -- `psql`), podzial z uwzglednieniem cudzyslowow, przekierowania i `$(...)` wymagaja potwierdzenia, flagi uruchamiajace programy (`git -c`, `--upload-pack`, `-O`, `sort --compress-program`) i piszace do plikow (`find -delete`, `curl -o`) tez
- `curl` do obcego hosta wymaga potwierdzenia (ochrona przed wyslaniem danych na zewnatrz)
- Restart i zatrzymanie uslug (`systemctl restart/stop`), `apt install` i `docker compose up/down` wymagaja teraz potwierdzenia
- Sekrety w wynikach narzedzi (klucze API, tokeny, JWT, hasla w URL-ach, hashe z shadow, klucze prywatne) sa ukrywane przed wyslaniem do providera LLM
- Potwierdzenie zapisu pliku pokazuje diff i ostrzega przy plikach startowych; znaki sterujace i bidi w potwierdzeniach sa widoczne jako `\xNN`
- Zapis skilla i pelne nadpisanie SERVER.md wymagaja potwierdzenia; kazdy inny zapis pamieci jest zglaszany komunikatem
- `AGENT_TOKEN` jest generowany automatycznie przez kreator i instalator; w trybie kubernetes jest wymagany. Token porownywany w stalym czasie, audit log odporny na falszowanie wpisow
- `backend/.env` nie trafia juz do obrazu Dockera (wczesniej `.dockerignore` byl w `.gitignore`)

### Poprawki

- `docker_manage` i `cron_manage` dzialaja: schemat narzedzia i handler uzywaly roznych nazw pol, wiec kazde wywolanie konczylo sie bledem albo `crontab -l`
- Kilka wywolan narzedzi w jednej odpowiedzi modelu z potwierdzeniem, nowa wiadomosc zamiast TAK/NIE i rozlaczenie klienta w trakcie narzedzia nie psuja juz historii sesji
- Komendy zatwierdzone przez uzytkownika maja limit 15 minut zamiast 30 s; timeout zabija cala grupe procesow
- Statystyki, porty i dyski czytane sa z `/proc` hosta bez uruchamiania komend (wszystkie dyski, obciazenie wzgledem liczby rdzeni)

### Architektura

- `agent.py` bez martwego kodu (~500 linii); wspolna petla dla agenta i workerow, zdarzenia strumienia (tekst, zalaczniki, postep)
- Protokol rozszerzony wstecznie zgodnie: ramki `attachment` i `event`, komendy `status`, `diagram`, `directory`, `vibe`, `alerts`, `targets`, `routines`, `history`, `investigate`, `subscribe`

### Dokumentacja

- Nowe: `docs/features.md`, `docs/deploy.md`, `deploy/kubernetes/README.md`; przepisane README, `docs/security.md`, `docs/protocol.md`, `CLAUDE.md`
- Testy: 555 (wczesniej 353), w tym petla agenta na atrapie LLM i regresje obejsc klasyfikatora

---

## v0.8.1 (2026-09-16)

### Poprawki

- Po zmianie katalogu (`change_directory`) komendy, git i docker znowu dzialaja. Wczesniej podanie sciezki z `/hostfs` zapisywalo ja podwojnie (`/hostfs/hostfs/...`) i kazda kolejna komenda padala, a zwykla sciezka hosta byla odrzucana jako nieistniejaca. Teraz dzialaja sciezki hosta, sciezki z `/hostfs` i sciezki wzgledne, a zablokowane katalogi (np. `/root/.ssh`) sa odrzucane
- `network_info` wreszcie dziala: wczesniej kazde wywolanie konczylo sie `ip addr show` kontenera. Porty i polaczenia pochodza teraz z sieci hosta VPS (`/hostproc/1/net`), a `ping`, `curl` i `dns` korzystaja z podanego celu
- `/server` (skan serwera) opisuje host, a nie kontener agenta: agent czyta konfiguracje z `/hostfs/etc`, porty hosta, dysk `/hostfs` i procesy hosta, a w SERVER.md zapisuje sciezki bez prefiksu `/hostfs`

### Wydajnosc

- Katalog roboczy trafia na koniec system promptu, wiec zmiana katalogu nie uniewaznia cache promptu u providera (SERVER.md i reguly pozostaja wspolnym prefiksem)

## v0.8.0 (2026-09-11)

### Komendy "/" w Telegramie i CLI

- Dodano `/server`: pokazuje SERVER.md, a gdy go nie ma -- agent bada serwer i tworzy plik. `/server aktualizuj` bada serwer ponownie
- Dodano `/skille` (lista skilli) i `/pomoc` (lista komend)
- Kazdy skill ma wlasna komende, np. `/odnow_certyfikat`; do komendy mozna dopisac wskazowki (`/odnow_certyfikat tylko dla example.com`). W CLI dziala tez forma z myslnikiem
- Telegram: bot sam ustawia menu podpowiedzi po wpisaniu `/` i odswieza je po kazdej wiadomosci, wiec nowy skill pojawia sie w menu od razu. Menu jest ustawiane osobno dla czatu kazdego dozwolonego uzytkownika -- osoby spoza listy nie widza nazw ani opisow skilli
- Telegram: na nieznana komende bot odpowiada podpowiedzia (wczesniej milczal)
- CLI: tekst zaczynajacy sie od `/`, ktory nie jest komenda (np. `/var/log jest pelny?`), nadal trafia do agenta jak zwykla wiadomosc

### Protokol

- Nowy typ zadania `{"command": ...}`: `list_skills`, `server_md`, `scan_server`, `run_skill`. Zmiana jest addytywna -- dotychczasowe klienty dzialaja bez zmian. Tresc wiadomosci dla agenta buduje backend, nie klient

### Poprawki

- Nazwa skilla to zawsze nazwa jego katalogu -- reczna edycja naglowka w SKILL.md nie psuje juz komendy ani odczytu

### Dokumentacja

- Opisano komendy w `docs/telegram.md` i `docs/cli.md`, nowy typ zadania w `docs/protocol.md`
- Dodano instrukcje korzystania z CLI bezposrednio na serwerze (`--no-tunnel`)
- Dodano 35 testow (komendy w backendzie i klientach); lacznie 332

---

## v0.7.0 (2026-09-11)

### Pamiec agenta

- Agent prowadzi wlasny `SERVER.md` -- notatki o serwerze (system, uslugi, kontenery, domeny, porty, wazne sciezki, decyzje uzytkownika), dolaczane do kazdej rozmowy. Nowe narzedzie `server_md`: odczyt, aktualizacja jednej sekcji i zapis calosci
- Agent moze tworzyc wlasne skille -- zapisane procedury wielokrotnego uzytku w formacie Agent Skills (`skills/<nazwa>/SKILL.md`). W prompcie jest tylko lista skilli, a tresc agent wczytuje, gdy zadanie pasuje do opisu. Nowe narzedzie `skill_manage`: lista, odczyt, zapis i usuwanie
- Pamiec lezy na serwerze w `backend/data/` (w kontenerze `/app/data`, zmienna `DATA_DIR`) i przetrwa restart oraz przebudowe obrazu. Katalog dodano do `.gitignore`
- Zapis do pamieci nie wymaga potwierdzenia, ale trafia do audit logu (sama sciezka, bez tresci). Zapis wygladajacy na sekret (klucz prywatny, klucz API, token, `DB_PASSWORD=...`) jest odrzucany, bo `SERVER.md` jest wysylany do providera LLM z kazdym zapytaniem
- Pamiec jest w prompcie oznaczona jako dane, nie polecenia -- nie zmienia klasyfikacji komend ani wymogu potwierdzen

### Dokumentacja

- Usunieto wzmianki o konkretnym dostawcy VPS -- dokumentacja, przyklady i instalatory mowia teraz po prostu o serwerze (przykladowy adres: `serwer.example.com`)
- Opisano pamiec agenta w `README.md`, `docs/backend.md` i `docs/security.md`
- Dodano 64 testy (pamiec agenta, kazde narzedzie ma handler); lacznie 297

---

## v0.6.0 (2026-09-11)

### Zmiana nazwy

- Projekt nazywa sie teraz **Pipe** -- nowa nazwa jest w dokumentacji, komunikatach, prompcie systemowym agenta, bocie Telegrama, kreatorze konfiguracji i banerze CLI
- Komenda CLI to teraz `pipe`. Instalatory (`install.sh`, `install.ps1`) przy ponownym uruchomieniu zastepuja w profilu powloki blok z poprzednia nazwa komendy, zamiast dopisywac drugi. Kto nie uruchomi instalatora ponownie, zachowa dzialajaca poprzednia komende
- Agent przedstawia sie teraz jako Pipe

### Dokumentacja

- Wyrownano ramke przykladowej sesji w `docs/quickstart.md`

---

## v0.5.1 (2026-09-11)

### Potwierdzenia operacji

- Naprawiono blad: w Telegramie potwierdzenie moglo pokazywac inna komende niz ta, ktora zostanie wykonana. Gwiazdki, podkreslniki i backslashe byly zamieniane na formatowanie (`rm /var/log/*.gz /tmp/*.old` wyswietlalo sie jako `rm /var/log/.gz /tmp/.old`), a komenda z `<` byla ucinana (`mysql produkcja < drop_all.sql` wyswietlalo sie jako `mysql produkcja`)
- Komendy w potwierdzeniach i odmowach sa pokazywane znak w znak -- w Telegramie i w CLI -- takze gdy zawieraja backticki (np. podstawienie komendy) albo kilka linii
- Potwierdzenie operacji Docker pokazuje pelna komende `docker ...`, ktora zostanie wykonana
- Bot wysyla poprawny HTML Telegrama: `<`, `>` i `&` poza tagami sa escapowane, wiec wiadomosc nie jest odrzucana
- Tryb awaryjny (zwykly tekst) usuwa juz tylko prawdziwe tagi, a nie wszystko miedzy `<` a `>`

### Cron

- Usunieto niebezpieczna sciezke w `cron_manage`: "usuniecie wpisu" zapisywalo do wykonania `crontab -r`, czyli wyczyszczenie calego crontaba, a od v0.4.0 taka komenda byla wykonywana po zatwierdzeniu. W praktyce sciezka byla nieosiagalna (patrz ograniczenie nizej). Usuwanie dotyczy teraz wylacznie wskazanej linii, dodawanie dopisuje wpis do istniejacego crontaba, wpis jest bezpiecznie cytowany, a zabronione polecenia w nim sa odrzucane
- Znane ograniczenie: `cron_manage` nadal nie dziala -- handler czyta inne nazwy parametrow niz te, ktore wysyla model, a obraz Dockera nie zawiera programu `crontab`

### Wewnetrzne

- Formatowanie odpowiedzi dla Telegrama wydzielono do `clients/telegram/tg_format.py` (testowalne bez biblioteki Telegrama)
- Dodano 82 testy (formatowanie w Telegramie i CLI, cron); lacznie 233

---

## v0.5.0 (2026-09-11)

### Providerzy LLM

- Dodano kreator konfiguracji `python3 -m backend.configure`: wybor providera, klucz API, lista modeli pobierana na zywo z API providera i test tool callingu przed zapisaniem `.env`. Kreator nie wymaga instalowania zadnych pakietow
- Dodano 14 wbudowanych providerow: Google Gemini, OpenAI, Anthropic Claude, OpenRouter, Groq, DeepSeek, Mistral, xAI Grok, Z.ai (GLM), Moonshot Kimi, Together AI, Cerebras, Fireworks i lokalna Ollama
- Wlasnego providera (dowolny endpoint zgodny z OpenAI: vLLM, LM Studio, LiteLLM, proxy firmowe) mozna dodac kreatorem -- trafia do `backend/data/providers.json`. Wpis o id wbudowanego providera nadpisuje go, wiec nieaktualny adres da sie poprawic bez nowej wersji Pipe
- Nowe zmienne: `LLM_PROVIDER`, `LLM_REASONING_EFFORT`, `LLM_TIMEOUT`. Klucz mozna tez podac przez zmienna providera, np. `OPENAI_API_KEY` albo `GEMINI_API_KEY`
- Dodano tryby `--check` (test obecnej konfiguracji), `--models` (aktualne modele) i `--providers`

### Aktualne modele

- Zmieniono domyslny model z `gemini-2.0-flash` na `gemini-3.8-flash`. Google wylaczyl `gemini-2.0-flash`, przez co swieza instalacja z domyslnym `.env` nie dzialala
- Backend przy starcie sprawdza w tle, czy skonfigurowany model jest nadal dostepny u providera, i wypisuje w logach ostrzezenie z propozycjami, jesli zostal wycofany
- Podniesiono domyslny limit czasu odpowiedzi LLM z 60 do 120 sekund -- modele rozumujace potrafia odpowiadac dluzej

### Zgodnosc

- Stare pliki `.env` (tylko `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`) dzialaja dalej -- provider jest rozpoznawany po adresie. Jesli masz w nim `LLM_MODEL=gemini-2.0-flash`, uruchom `python3 -m backend.configure`
- Agent nie wysyla juz parametru `tool_choice` -- przy podanych narzedziach i tak domyslnie jest `auto`, a czesc providerow (np. Ollama) go nie obsluguje
- W `docker-compose.yml` dodano `host.docker.internal` (dostep do Ollamy na hoscie) i przekazywanie nowych zmiennych

### Dokumentacja i testy

- Przepisano sekcje providerow w `docs/backend.md`; instalacja w `README.md` i `docs/quickstart.md` zaczyna sie od kreatora
- Dodano 57 testow (rejestr providerow, rozwiazywanie konfiguracji, kreator); lacznie 151

---

## v0.4.1 (2026-09-09)

### Odpowiedzi agenta

- Handlery narzedzi nie wysylaja juz do uzytkownika surowego outputu komend. Wczesniej ten sam wynik trafial dwa razy: raz jako surowy STDOUT z kodem wyjscia, raz jako odpowiedz sformulowana przez model -- mimo ze system prompt wprost zabrania modelowi zwracania surowego outputu
- Do uzytkownika trafiaja teraz wylacznie komunikaty protokolu (`[POTWIERDZ]`, `[ODMOWA]`) oraz odpowiedz modelu; pelny wynik komendy nadal dostaje model
- Odmowa dostepu w `read_file` jest zglaszana uzytkownikowi w formacie `[ODMOWA]`, spojnie z `write_file`

### Poprawki

- `system_stats` dla konkretnego typu statystyki (`cpu`, `memory`, `disk`, `process`) przekazywal modelowi jedynie komunikat "Statystyki pobrane" bez danych -- model dostaje teraz faktyczny odczyt
- `change_directory` nie raportuje juz "Katalog zmieniony" po nieudanej zmianie katalogu; do modelu trafia rzeczywisty wynik operacji

---

## v0.4.0 (2026-09-09)

### Naprawa tool callingu

- Naprawiono krytyczny blad: handlery w `backend/core/handlers/` odwolywaly sie do nieistniejacego `agent._executor`, przez co **kazde** wywolanie narzedzia przez LLM konczylo sie bledem. Handlery korzystaja teraz bezposrednio z modulu `backend.core.executor`
- Przywrocono prefiks `/hostfs` przy katalogu roboczym komend i operacji Git -- po refaktorze komendy uruchamialy sie w sciezce kontenera zamiast w sciezce hosta VPS
- Potwierdzone operacje `git_command`, `docker_manage` i `cron_manage` nie koncza sie juz komunikatem "Nieznana operacja" -- `_execute_tool_confirmed` obsluguje kazde narzedzie przechowujace gotowa komende shell

### Autoryzacja tokenem

- `AGENT_TOKEN` faktycznie dziala: brakowalo definicji w `config/settings.py`, wiec dotychczasowa kontrola tokenu byla martwa
- Token jest sprawdzany przed kazda akcja, takze przed potwierdzeniem operacji -- wczesniej nieuwierzytelniony klient mogl zatwierdzic komende oczekujaca w cudzej sesji
- CLI przyjmuje token przez `--token` lub zmienna `AGENT_TOKEN`
- Bot Telegrama przyjmuje token przez `AGENT_TOKEN` w `clients/telegram/.env`
- `AGENT_TOKEN` przekazywany jest do obu serwisow w `docker-compose.yml`

### Wersjonowanie

- Dodano plik `VERSION` w korzeniu repo jako jedyne zrodlo prawdy o wersji
- Dodano `scripts/bump_version.py` -- podbija wersje we wszystkich miejscach, gdzie byla wpisana na sztywno
- Ujednolicono format wersji do `X.Y.Z` (`v0.3` -> `v0.3.0`)
- Dodano skill `/ship` (`.claude/skills/ship/SKILL.md`) opisujacy proces wydania: wersja, changelog, dokumentacja, testy, commit, push

### Testy

- Naprawiono `backend/tests/test_executor.py`, ktory importowal nieistniejaca klase `LocalExecutor` i blokowal zbieranie calego zestawu testow
- Poprawiono bledna asercje w tescie nieistniejacej komendy (powloka zwraca 127 i "command not found")
- Caly zestaw: 94 testy przechodza

### Dokumentacja

- Przepisano `CLAUDE.md`: poprawiono opis protokolu JSON, dodano opis `core/handlers/`, `core/session.py`, konwencji `/hostfs` oraz kontraktu handlerow
- Uzupelniono `docs/protocol.md`, `docs/backend.md`, `docs/cli.md`, `docs/telegram.md` i `README.md` o autoryzacje tokenem
- Poprawiono tabele modulow w `README.md` (`core/executor.py` nie zawiera klasy `LocalExecutor`)

---

## v0.3 (2026-06-19)

### Naprawa statystyk systemowych

- Naprawiono krytyczny blad: statystyki systemu (RAM, CPU, uptime, load average) byly odczytywane z maszyny fizycznej hostujucej VPS zamiast z samego VPS-a
- Usunieto mechanizm `nsenter` ktory uciekal z izolacji LXC VPS-a na maszyne fizyczna
- Usunieto stary mechanizm "teleportacji" statystyk przez cron do `/tmp/vps_*`
- Statystyki sa teraz odczytywane bezposrednio z `/hostproc` (zamontowany `/proc` hosta VPS)

### Konfiguracja Docker

- Dodano montowanie `/proc:/hostproc:ro` w docker-compose dla dostepu do procfs hosta VPS
- Dodano `pid: host` do kontenera vps-agent -- wspoldzielenie PID namespace z VPS-em zapewnia prawidlowe odczyty uptime i loadavg
- Zaktualizowano fake `free` w Dockerfile aby czytal z `/hostproc/meminfo`

### System prompt

- Zaktualizowano system prompt agenta -- usinieto przestarzale odniesienia do teleportacji statystyk
- Dodano informacje o nowej architekturze `/hostproc` i `pid: host`

### Dokumentacja

- Uaktualniono w calym repozytorium wersje do v0.3

---

## v0.2 (2026-03-29)

### Nawigacja po serwerze
- Agent sledzi swoj stan z katalogiem roboczym na hoście (wlasciwosc `cwd` w `Session`)
- Nowe narzedzie `change_directory` umozliwiajace latwe zmienianie polozenia `/hostfs/...`
- Automatyczne powiadamianie uzytkownika o aktualnym polozeniu (tag `[Katalog: ...]`)

### Narzedzia systemowe
- Zainstalowanie w obrazie `git`, `dnsutils`, `iputils-ping` poprawiajace dzialanie `network_info` i `git_command`

### Dokumentacja
- Przeredagowano README odrzucajac zbedny diagram i dodajac wzmianke o multiplatformowosci (Telegram/Discord/CLI)
- Uaktualniono w calym repozytorium wersje do v0.2

---

## v0.1 (2026-03-29)

Pierwsza wersja Pipe -- autonomiczny agent AI do zarzadzania serwerem VPS.

### Narzedzia agenta

- `execute_command` -- wykonywanie komend shell na serwerze
- `read_file` / `write_file` -- odczyt i zapis plikow
- `git_command` -- zarzadzanie repozytoriami Git (status, log, diff, commit, push, stash)
- `system_stats` -- szczegolowe statystyki systemowe z /proc (CPU, RAM, dysk, procesy)
- `docker_manage` -- zarzadzanie kontenerami i obrazami Docker (ps, logs, inspect, stats, restart, stop, prune)
- `network_info` -- diagnostyka sieciowa (otwarte porty, polaczenia, ping, curl, DNS)
- `cron_manage` -- zarzadzanie zadaniami cron (lista, dodawanie, usuwanie, logi)

### Interfejsy

- CLI -- interaktywny terminal z automatycznym tunelem SSH
- Telegram Bot -- zarzadzanie serwerem przez Telegram (HTML formatting)
- Discord -- placeholder (PR welcome)
- Web UI -- placeholder (PR welcome)

### Bezpieczenstwo

- Trzystopniowa klasyfikacja komend: safe / confirm / forbidden
- Append-only audit log
- Izolacja w kontenerze Docker
- Whitelist uzytkownikow Telegram
- Read-only montowanie hosta pod /hostfs

### Backend

- Petla LLM z tool calling (max 10 iteracji)
- Wsparcie dla wielu providerow LLM (Gemini, OpenAI, Groq, Anthropic)
- Unix socket + TCP listener
- Zarzadzanie sesjami per uzytkownik

### Formatowanie Telegram

- Przejscie z MarkdownV2 na HTML parse mode
- Eliminacja problemow z escapowaniem znakow specjalnych
- Automatyczna konwersja resztek Markdowna na HTML
- Fallback na plain text przy bledach parsowania

### Statystyki systemowe

- Zuzycie RAM z /proc/meminfo (nie z uproszczonego `free`)
- Obciazenie CPU z /proc/stat i /proc/loadavg
- Top 10 procesow po CPU/RAM
- Zuzycie dysku (wlaczajac montowanie hosta)

### Dokumentacja

- Struktura docs/ z oddzielnymi plikami dla kazdego komponentu
- Brak emotikon w dokumentacji
- Poprawiona skladnia i gramatyka
