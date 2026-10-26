# Pipe Web

Interfejs w przegladarce: rozmowa z agentem, schemat serwera na zywo i podglad zmian.

```bash
pipe web                 # tunel SSH jak w CLI + strona na http://127.0.0.1:7400
pipe web --lang en       # po angielsku
pipe web --no-browser    # tylko wypisz adres
pipe web --web-port 8080
```

Adres wypisany w terminalu zawiera jednorazowy klucz dostepu. Nic nie trzeba budowac ani instalowac --
strona to zwykle pliki z `static/`, a serwer (`server.py`) uzywa wylacznie biblioteki standardowej Pythona.

## Jak to dziala

```
przegladarka --HTTP--> 127.0.0.1:7400 (server.py na Twoim laptopie) --tunel SSH--> backend na serwerze
```

- Na serwerze nie otwiera sie zaden nowy port; token backendu zostaje w procesie `pipe web`.
- `POST /api/request` przekazuje zadanie protokolu Pipe i strumieniuje ramki (NDJSON),
  `GET /api/events` przekazuje alerty, przypomnienia i raporty na zywo (SSE).
- Strona jest dostepna tylko z tego komputera, z kluczem z adresu (potem ciasteczko HttpOnly);
  zadania z obcym naglowkiem `Host` albo `Origin` sa odrzucane.

## Pierwsze uruchomienie

`pipe web` bez `--host` i bez zapisanego polaczenia (albo z `--setup`) otwiera kreator: *Co chcesz robic z Pipe?*
(kilka slow albo wybor widoku prostego lub technicznego), potem *Gdzie ma dzialac Pipe?* -- na tym komputerze (Docker)
albo na Twoim serwerze (adres, port, uzytkownik i haslo, plik klucza albo klucze z `~/.ssh`). Most (`server.py`,
`POST /api/setup`) wola `clients/cli/appsetup.py` i pokazuje postep linia po linii; po sukcesie przelacza sie na nowy
backend, a strona odswieza sie sama. Haslo idzie tylko do tego procesu i nie jest zapisywane. Klikniecie adresu serwera
w naglowku otwiera drugi krok ponownie (zmiana miejsca).

## Co jest na stronie

- **Widok** -- przycisk w naglowku przelacza widok prosty (rozmowa, *Pulpit* bez wykresow, *Dodatki*) i techniczny
  (do tego *Schemat* i *Zmiany*). Wybor jest wspolny z CLI i Telegramem (`/widok`).
- **Dodatki** -- skille (uruchom, podglad), podlaczone serwery MCP i dodawanie nowych z GitHuba (`/pobierz`, `/mcp dodaj`).
- **Pulpit** (pierwsza zakladka) -- bez modelu: naglowek *Wszystko dziala* albo co wymaga uwagi, pierscienie
  procesora, pamieci i dysku z przebiegiem z ostatniej doby, skrzynka *Od Pipe* (alerty, ustalenia, podpowiedzi
  i raporty z przyciskami akcji; dzwonek w gornym pasku liczy nowe i otwiera ustawienia powiadomien), karty projektow
  z warsztatu (logi, zatrzymanie, *Z GitHuba*) i zuzycie modelu dzis. Wiadomosc z `push` pokazuje sie tez jako okienko.
- **Rozmowa** -- pusty ekran podsuwa zadania zwyklymi slowami (te oznaczone *bez AI* dzialaja od razu i nic nie kosztuja); odpowiedzi agenta, diagramy, potwierdzenia z planem bezpiecznika i roznica pliku.
- **Zalaczniki i glos** -- spinacz, przeciagniecie pliku albo wklejenie zrzutu ekranu dolacza pliki do wiadomosci
  (miniatury przed wyslaniem, do 10 MB na plik); mikrofon nagrywa glosowke (WAV 16 kHz) i wysyla jej tekst.
- **Schemat** -- generowany automatycznie, trzy poziomy: serwery -> wnetrze serwera -> projekt compose.
  Element, na ktorym agent pracuje, jest podswietlony; *Sledze agenta* przenosi widok za nim. Dowolny ruch
  na schemacie wylacza sledzenie, przycisk wlacza je z powrotem. Pod schematem os czasu dzialan.
- **Zmiany** -- dziennik zatwierdzonych zmian: roznica "przed -> po" dla kazdego pliku i przycisk cofniecia.
- **Provider** -- przy pierwszym wejsciu ekran wyboru providera LLM (klucz API, sprawdzenie, model). Nad polem
  rozmowy przelacznik: lista providerow, do ktorych jest klucz, i przelaczenie jednym kliknieciem. `/provider`
  otwiera ekran ponownie. Logotypy providerow (`static/providers/`, pakiet `@lobehub/icons-static-svg`, MIT) sa znakami towarowymi swoich wlascicieli i sluza tylko wskazaniu, z czyimi modelami laczy sie agent -- patrz `static/providers/NOTICE.md`; wlasni providerzy maja monogram. Klucze sa zapisywane na serwerze (`DATA_DIR/llm_keys.json`), nie w przegladarce.
- **Komendy** -- wpisz `/` w polu rozmowy: rozwija sie lista wszystkich komend i skilli z opisami
  (strzalki, Enter uruchamia, Tab uzupelnia, Esc zamyka). Te same komendy co w CLI, po polsku i po angielsku.
  Skille uruchamiasz stad (`/skille` pokazuje liste z trescia); alerty sa w skrzynce na pulpicie i w `/alerty`.

## Pliki

| Plik | Rola |
|------|------|
| `server.py` | lokalny serwer HTTP i most do backendu |
| `static/app.js` | czat, potwierdzenia, os czasu, zakladki |
| `static/home.js` | pulpit: stan serwera, skrzynka z akcjami, projekty, ustawienia powiadomien |
| `static/onboard.js` | pierwsze uruchomienie: widok, miejsce (lokalnie / serwer), postep instalacji |
| `static/addons.js` | zakladka Dodatki: skille i serwery MCP |
| `static/graph.js` | schemat: uklad, kamera, przejscia miedzy poziomami, animacje |
| `static/md.js` | bezpieczny renderer Markdowna (bez `innerHTML`) |
| `static/i18n.js` | teksty pl / en |
| `static/app.css` | wyglad: jasny i ciemny motyw, kolor tylko z funkcja (niebieski -- agent pracuje, bursztyn -- czeka na zgode, zielony / czerwony -- wynik) |
| `static/fonts/` | IBM Plex Sans i IBM Plex Mono (`woff2`, licencja SIL OFL 1.1 w `OFL.txt`) -- strona nie laduje nic z zewnatrz |
