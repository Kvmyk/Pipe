---
name: ship
description: Wydaje zmiany w PipeClaw — podbija wersje wedlug wagi zmiany, aktualizuje changelog i dokumentacje, commituje i pushuje. Uzyj gdy uzytkownik mowi "ship", "wydaj", "zacommituj i wypchnij", "wypusc wersje" albo konczy prace nad zmiana i chce ja oddac do repo.
---

# /ship — wydanie zmian PipeClaw

Kolejnosc jest scisla: **wersja → dokumentacja → testy → commit → push**.
Nigdy nie commituj przed podbiciem wersji i uzupelnieniem changelogu.

## 1. Zbadaj zakres zmian

```bash
git status --short
git diff --stat
git diff            # oraz `git diff --cached` jesli cos jest juz w indeksie
```

Jesli nie ma zadnych zmian — powiedz o tym i zakoncz. Nic nie commituj „na wszelki wypadek".

Przeczytaj faktyczna trescia diffa. Wielkosc podbicia wynika z tego, co sie zmienilo,
a nie z liczby linii.

## 2. Wybierz wielkosc podbicia

Zrodlem prawdy jest plik `VERSION` w korzeniu repo (format `X.Y.Z`).

| Podbicie | Kiedy | Przyklady z tego projektu |
|----------|-------|---------------------------|
| **patch** `0.3.0 → 0.3.1` | poprawki bledow, literowki, drobne poprawki dokumentacji, refaktor bez zmiany zachowania | naprawa handlera, poprawiony komunikat, uzupelniony test |
| **minor** `0.3.0 → 0.4.0` | nowa funkcjonalnosc wstecznie zgodna, nowe narzedzie agenta, nowy klient, zauwazalna zmiana zachowania | dodanie narzedzia do `tools.py`, wsparcie tokenu w kliencie, nowa regula w `security.py` |
| **major** `0.3.0 → 1.0.0` | zmiana lamiaca zgodnosc lub przebudowa architektury | zmiana protokolu JSON, zmiana kontraktu handlerow, usuniecie/zmiana nazwy narzedzia, zmiana ukladu wolumenow w Dockerze |

Zasady rozstrzygajace watpliwosci:
- Zmiana protokolu druciarskiego (`backend/server.py` ↔ klienci) albo formatu `.env`, ktora
  wymaga od uzytkownika czegokolwiek zrobic recznie przy aktualizacji → **major**.
- Kilka zmian na raz → decyduje najwieksza z nich.
- Nie masz pewnosci miedzy dwoma poziomami → wybierz nizszy i napisz uzasadnienie
  w podsumowaniu dla uzytkownika.

Podbij:

```bash
python scripts/bump_version.py patch     # albo minor / major / X.Y.Z
```

Skrypt aktualizuje `VERSION` oraz wszystkie miejsca, gdzie wersja jest wpisana na sztywno
(README, `docs/*.md`, docstringi backendu, `prompts.py`, banner CLI, bot Telegrama).
Jesli wypisze `[uwaga] wzorzec nie pasuje` — popraw ten plik recznie i dopisz wzorzec
do listy `PATTERNS` w skrypcie, zeby nastepnym razem zadzialal sam.

## 3. Zaktualizuj dokumentacje

**`docs/changelog.md` jest obowiazkowy.** Dopisz nowa sekcje na gorze, nad poprzednia wersja,
w formacie uzywanym w tym pliku (naglowek `## vX.Y.Z (RRRR-MM-DD)`, pod nim naglowki `###`
grupujace zmiany tematycznie, pod nimi punkty). Pisz po polsku, w czasie przeszlym,
o tym co sie zmienilo dla uzytkownika — nie streszczaj diffa linia po linii.

Nastepnie sprawdz, czy zmiana nie unieważnila innych dokumentow, i popraw te, ktorych dotyczy:

| Co zmieniles | Co zaktualizowac |
|--------------|------------------|
| protokol JSON, `server.py` | `docs/protocol.md`, sekcja protokolu w `README.md`, `CLAUDE.md` |
| `security.py`, klasyfikacja komend | `docs/security.md`, sekcja bezpieczenstwa w `README.md` |
| narzedzia agenta (`tools.py`, `handlers/`) | tabela narzedzi w `README.md`, lista narzedzi w `config/prompts.py`, `CLAUDE.md` |
| zmienne `.env`, `docker-compose.yml` | `backend/.env.example`, `docs/backend.md`, `docs/quickstart.md`, `CLAUDE.md` |
| CLI (`clients/cli/`) | `docs/cli.md`, `clients/cli/README.md` |
| Telegram (`clients/telegram/`) | `docs/telegram.md`, `clients/telegram/.env.example` |
| architektura, uklad modulow | `CLAUDE.md`, `AGENTS.md` |

Cala dokumentacja i komentarze w tym repo sa **po polsku** — trzymaj sie tego.

## 4. Uruchom testy

```bash
python -m pytest backend/tests/ -q
```

Musza przejsc wszystkie. Jesli ktorys pada:
- padl przez Twoja zmiane → napraw kod (albo test, jesli to test byl zly) przed commitem;
- padl juz wczesniej i nie dotyczy Twojej zmiany → zglos to uzytkownikowi i zapytaj,
  czy mimo to wydac.

Nie commituj przy czerwonych testach bez wyraznej zgody uzytkownika.

## 5. Commit

Sprawdz, co dokladnie wchodzi do commita, i **nigdy nie dodawaj sekretow**:
`backend/.env`, `clients/telegram/.env`, `audit.log`, `backend/data/`.
Wersjonowane sa wylacznie pliki `.env.example`.

```bash
git status --short
git add <konkretne pliki>     # nie `git add -A` w ciemno
```

Komunikat commita — po polsku, w trybie rozkazujacym, z prefiksem typu:

```
<typ>: <krotki opis zmiany> (vX.Y.Z)

<opcjonalny akapit: co i dlaczego, jesli nie widac tego z tytulu>
```

Typy: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`.

Jesli biezaca galaz to `main`, a zmiana jest wieksza niz patch — zaproponuj wczesniej
zalozenie galezi i PR zamiast commitowania prosto na `main`.

## 6. Push

Push jest widoczny na zewnatrz i trudny do cofniecia, wiec:

- **Zapytaj uzytkownika o zgode przed pierwszym pushem w sesji**, pokazujac galaz i cel
  (`git push -u origin <galaz>`). Jesli w tej samej sesji uzytkownik juz raz wyrazil zgode
  na push tej galezi, kolejne pushe tej samej galezi rob bez pytania.
- Nigdy nie uzywaj `--force` ani `--force-with-lease` bez wyraznej prosby uzytkownika.

```bash
git push -u origin <galaz>
```

## 7. Podsumuj

Krotko, po polsku: nowa wersja, typ podbicia wraz z uzasadnieniem, lista zaktualizowanych
dokumentow, wynik testow, hash commita i to, czy push doszedl do skutku.
