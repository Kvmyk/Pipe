# Plan prac -- Pipe

## Zamrozenie funkcji (od v0.31)

Nowych funkcji nie dodajemy, dopoki Pipe nie ma uzytkownikow spoza autora. Priorytety w tej kolejnosci:

1. Bledy i bezpieczenstwo (`SECURITY.md`), takze zgloszone przez uzytkownikow.
2. Prostsza pierwsza instalacja: piaskownica (`sandbox/`), tryb bez modelu, profil obserwacji, gotowe obrazy.
3. Dowody, ze to dziala: wyniki `python3 sandbox/run.py eval` dla popularnych modeli, nowe scenariusze awarii.
4. Funkcje oznaczone w dokumentacji jako eksperymentalne (zdalne cele, workery, glosowki, Kubernetes) zostaja
   (decyzja z 2026-10-26): workery napedzaja rutyny i samodzielne badanie alertow, cele ssh -- `/zalataj serwery`,
   Kubernetes -- wdrozenie na istniejacym klastrze. Etykieta "eksperymentalne" znika, gdy ktos uzyje ich na
   prawdziwym serwerze.

Wyjatki: zmiana, o ktora prosi wiecej niz jedna osoba korzystajaca z Pipe na prawdziwym serwerze, oraz zmiany
z punktu 1 -- dlatego ponizsze lacenie podatnosci wchodzi mimo zamrozenia. Decyzja autora byla tez przebudowa
z v0.39 (Pipe pisze pierwszy, warsztat, narzedzia na zadanie, pulpit w `pipe web`) oraz widoki prosty i techniczny
z kreatorem pierwszego uruchomienia w `pipe web` (2026-10-26) -- kolejne funkcje znow tylko wedlug punktow wyzej.

## Dalej: podatnosci i latki

Wykrywanie podatnosci w pakietach hosta, obrazach kontenerow i zaleznosciach aplikacji oraz latki (`/zalataj`,
`/zalataj obrazy`) weszly w v0.32 i v0.33. Od v0.38 `/zalataj` lata od razu, bez pytania o kazda komende, takze
zdalne serwery i obrazy bez nowszej wersji (poprawki zainstalowane w obrazie), i sprzata po sobie -- opis
w `docs/features.md` (Audyt bezpieczenstwa).

Zostalo, tylko jesli ktos o to poprosi:

- **`PATCH_AUTO=off|security|all`** (domyslnie `off`) z oknem `PATCH_WINDOW` -- `/zalataj` samo, bez polecenia,
  z raportem po fakcie (`core/patching.py` juz dziala w tle i dostarcza wynik jako wiadomosc). Rozsadne glownie dla
  obrazow kontenerow, bo tylko tam jest prawdziwy powrot.
- Nowsze obrazy dla kontenerow bez compose (dzis tylko podpowiedz: `docker run` trzeba powtorzyc z tymi samymi
  opcjami -- odtworzenie przez API Dockera musi zachowac anonimowe wolumeny i nadpisane w `docker run` ustawienia
  obrazu).
- Zaleznosci aplikacji poza zakresem wersji z manifestu (np. przechodnia przypieta przez inna paczke) -- dzis zostaja
  w raporcie do zmiany recznej; mozliwe `overrides` (npm) / `resolutions` (yarn).
- Obrazy kontenerow na zdalnych serwerach (dzis tylko ich pakiety systemu).
