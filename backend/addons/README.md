# Katalog dodatkow MCP

Gotowe integracje, ktore uzytkownik wlacza jednym kliknieciem (`/mcp` w CLI, `pipe web` i na Telegramie). Nic
nie jest wlaczone z gory. Jeden plik JSON = jeden dodatek; nazwa pliku = `name`.

Dodajesz nowy? Skopiuj `github.json` i zmien:

- `name` -- male litery, cyfry, `-`, `_` (do 24 znakow); tak bedzie sie nazywal serwer i grupa narzedzi `mcp:<name>`.
- `title`, `description` -- po polsku i angielsku, zwyklymi slowami: co uzytkownik z tym zrobi.
- `server` -- to samo, co przyjmuje `/mcp dodaj`: `command` `npx`/`uvx` z argumentami, `image` (obraz Dockera)
  albo `url` (serwer HTTP). Przypnij wersje paczki (`paczka@1.2.3`). Serwer dziala w izolowanym kontenerze: bez
  plikow serwera, bez docker.sock, jako uzytkownik nobody.
- `env` -- sekrety tylko jako `${MCP_NAZWA}`; kazdy taki sekret opisz w `secrets` (etykieta, jak go zdobyc, link).
  Uzytkownik wpisuje go w oknie `/mcp` w `pipe web` albo w CLI, nigdy w rozmowie z modelem.
- `state: true` -- serwer dostaje wlasny wolumen `/state` (np. tokeny po logowaniu kodem urzadzenia). Usuniecie
  dodatku usuwa tez wolumen.
- `autoApprove` -- tylko narzedzia, ktore niczego nie zmieniaja (logowanie, stan logowania). Reszta pyta o zgode,
  chyba ze serwer sam oznaczy narzedzie jako odczyt (`readOnlyHint`).
- `login` -- opcjonalnie: co zrobic po dodaniu (np. "napisz: zaloguj mnie do ...").
- `category` -- `dev`, `office` albo `other` (kolejnosc na liscie).

`backend/tests/test_addons.py` sprawdza kazdy plik ta sama walidacja co `/mcp dodaj`.
