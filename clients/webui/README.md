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

## Co jest na stronie

- **Rozmowa** -- odpowiedzi agenta, diagramy, potwierdzenia z planem bezpiecznika i roznica pliku.
- **Schemat** -- generowany automatycznie, trzy poziomy: serwery -> wnetrze serwera -> projekt compose.
  Element, na ktorym agent pracuje, jest podswietlony; *Sledze agenta* przenosi widok za nim. Dowolny ruch
  na schemacie wylacza sledzenie, przycisk wlacza je z powrotem. Pod schematem os czasu dzialan.
- **Zmiany** -- dziennik zatwierdzonych zmian: roznica "przed -> po" dla kazdego pliku i przycisk cofniecia.
- **Skille** -- lista zapisanych procedur z podgladem tresci i przyciskiem uruchomienia.
- **Alerty** -- aktywne alerty z przyciskiem *Zbadaj* oraz zdarzenia na zywo.
- **Provider** -- przy pierwszym wejsciu ekran wyboru providera LLM (klucz API, sprawdzenie, model). Nad polem
  rozmowy przelacznik: lista providerow, do ktorych jest klucz, i przelaczenie jednym kliknieciem. `/provider`
  otwiera ekran ponownie. Znaczki przy providerach to wlasne monogramy, nie logotypy firm (znaki towarowe). Klucze sa zapisywane na serwerze (`DATA_DIR/llm_keys.json`), nie w przegladarce.
- **Komendy** -- wpisz `/` w polu rozmowy: rozwija sie lista wszystkich komend i skilli z opisami
  (strzalki, Enter uruchamia, Tab uzupelnia, Esc zamyka). Te same komendy co w CLI, po polsku i po angielsku.

## Pliki

| Plik | Rola |
|------|------|
| `server.py` | lokalny serwer HTTP i most do backendu |
| `static/app.js` | czat, potwierdzenia, os czasu, zakladki |
| `static/graph.js` | schemat: uklad, kamera, przejscia miedzy poziomami, animacje |
| `static/md.js` | bezpieczny renderer Markdowna (bez `innerHTML`) |
| `static/i18n.js` | teksty pl / en |
