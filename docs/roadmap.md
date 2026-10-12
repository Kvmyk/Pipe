# Plan prac -- Pipe

## Zamrozenie funkcji (od v0.31)

Nowych funkcji nie dodajemy, dopoki Pipe nie ma uzytkownikow spoza autora. Priorytety w tej kolejnosci:

1. Bledy i bezpieczenstwo (`SECURITY.md`), takze zgloszone przez uzytkownikow.
2. Prostsza pierwsza instalacja: piaskownica (`sandbox/`), tryb bez modelu, profil obserwacji, gotowe obrazy.
3. Dowody, ze to dziala: wyniki `python3 sandbox/run.py eval` dla popularnych modeli, nowe scenariusze awarii.
4. Funkcje oznaczone w dokumentacji jako eksperymentalne (zdalne cele, workery, glosowki,
   Kubernetes) dojrzewaja albo znikaja, zaleznie od tego, czy ktos ich uzywa.

Wyjatki: zmiana, o ktora prosi wiecej niz jedna osoba korzystajaca z Pipe na prawdziwym serwerze, oraz zmiany
z punktu 1 -- dlatego ponizsze lacenie podatnosci wchodzi mimo zamrozenia.

## Dalej: podatnosci i latki

Wykrywanie podatnosci w pakietach hosta, obrazach kontenerow i zaleznosciach aplikacji oraz latki na potwierdzenie
(`/zalataj`, `/zalataj obrazy`) weszly w v0.32 i v0.33 -- opis w `docs/features.md` (Audyt bezpieczenstwa).

Zostalo, tylko jesli ktos o to poprosi:

- **`PATCH_AUTO=off|security|all`** (domyslnie `off`) z oknem `PATCH_WINDOW` -- latki bez pytania, ta sama sciezka
  co YOLO (`_execute_tool_confirmed`, ten sam plan i dziennik), raport po fakcie. Rozsadne glownie dla obrazow
  kontenerow, bo tylko tam jest prawdziwy powrot.
- Nowsze obrazy dla kontenerow bez compose (dzis tylko podpowiedz: `docker run` trzeba powtorzyc z tymi samymi
  opcjami) i latki zaleznosci aplikacji (dzis tylko raport -- zmiana wersji to zmiana w kodzie aplikacji).
