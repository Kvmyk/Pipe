# Plan prac -- Pipe

## Zamrozenie funkcji (od v0.31)

Nowych funkcji nie dodajemy, dopoki Pipe nie ma uzytkownikow spoza autora. Priorytety w tej kolejnosci:

1. Bledy i bezpieczenstwo (`SECURITY.md`), takze zgloszone przez uzytkownikow.
2. Prostsza pierwsza instalacja: piaskownica (`sandbox/`), tryb bez modelu, profil obserwacji, gotowe obrazy.
3. Dowody, ze to dziala: wyniki `python3 sandbox/run.py eval` dla popularnych modeli, nowe scenariusze awarii.
4. Funkcje oznaczone w dokumentacji jako eksperymentalne (zdalne cele, workery, glosowki, Pipe jako klient MCP,
   Kubernetes) dojrzewaja albo znikaja, zaleznie od tego, czy ktos ich uzywa.

Wyjatki: zmiana, o ktora prosi wiecej niz jedna osoba korzystajaca z Pipe na prawdziwym serwerze, oraz zmiany
z punktu 1 -- dlatego ponizsze lacenie podatnosci wchodzi mimo zamrozenia.

## Dalej: podatnosci i latki

Pipe sam znajduje znane podatnosci w tym, co jest zainstalowane na serwerze, mowi o nich i proponuje latke.
Domyslnie zawsze pyta przed instalacja; automatyczne latanie tylko po swiadomym wlaczeniu.

Zalozenia:

- Skan jest deterministyczny (bez LLM) i bez kluczy API: OSV.dev `/v1/querybatch` (same ID), szczegoly tylko dla
  nowych ID; priorytet z CISA KEV (aktywnie wykorzystywane) i severity.
- Raportujemy tylko podatnosci z dostepna poprawka. Te bez poprawki co najwyzej jako liczba -- inaczej zwykly
  Debian daje setki pozycji.
- W trybie docker system hosta jest tylko do odczytu: Pipe wykrywa i daje gotowa komende do wykonania na hoscie
  (`host_only`, jak dzis w audycie). Latanie pakietow systemu tylko w trybie native.
- Pakietow systemowych nie da sie wiarygodnie cofnac (starej wersji czesto nie ma w repo) -- potwierdzenie mowi to
  wprost; bezpiecznik robi post-check (uslugi, strony). Prawdziwy rollback jest tylko dla obrazow kontenerow.
- Nie dublujemy `unattended-upgrades` -- audyt dalej je poleca; wartosc Pipe to wykrycie, kontekst (czy podatna
  usluga jest publiczna) i jedno potwierdzenie przez bezpiecznik z wpisem w dzienniku.

Etapy:

1. **Wykrywanie** -- zrobione (`core/vulns.py`), czeka na wydanie. Inwentarz pakietow systemu (`snapshots.packages()` + pole `Source:` z dpkg, bo OSV dla
   Debiana/Ubuntu szuka po pakiecie zrodlowym; apk dla Alpine) -> OSV batch -> stan znanych ID w `DATA_DIR`.
   - Czuwanie: pelny skan, gdy migawka pokaze zmiane w `packages`/`containers` ("nowa aplikacja"), oraz ponowne
     odpytanie OSV co `VULN_INTERVAL` (domyslnie 6 h, "nowa podatnosc"). Alert tylko dla nowych ID.
   - Nowa grupa alertow `vuln` z wlasnym `scope`; alert znika, gdy pakiet zostanie podbity. Natychmiast tylko KEV
     i wysoka severity, reszta w porannym digescie.
   - `/audyt` (`posture.run_audit`) pokazuje podatnosci jako zwykle pozycje z ocena i gotowa komenda
     (`apt-get install --only-upgrade ...`), wiec uzytkownik moze je ogarnac sam, zanim przyjdzie alert ("napraw 1").
     Audyt korzysta z ostatniego wyniku skanu; gdy jest stary, skanuje od nowa (z limitem czasu).
2. **Latka na potwierdzenie** -- zrobione, czeka na wydanie. Komenda `patch` bez LLM (podglad z tokenem, potem
   wykonanie przez bezpiecznik), `/zalataj` we wszystkich klientach, przycisk "Zalataj" przy alercie (Telegram,
   `pipe web`). Native: instalacja; docker i viewer: tylko podglad z komenda dla hosta.
3. **Kontenery.** Pakiety w obrazach (`/hostproc/<pid>/root/var/lib/dpkg/status`, apk), nowszy obraz przez
   `docker pull` z powrotem do poprzedniego image ID; zaleznosci aplikacji z lockfile'i repozytoriow z DIRECTORY.
4. **`PATCH_AUTO=off|security|all`** (domyslnie `off`) z oknem `PATCH_WINDOW` -- ta sama sciezka co YOLO
   (`_execute_tool_confirmed`, ten sam plan i dziennik), raport po fakcie.
