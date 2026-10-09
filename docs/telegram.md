# Telegram Bot -- Pipe

Pipe v0.30.1

Bot Telegram do zarzadzania serwerem VPS przez agenta AI.

## Opis

Bot Telegram to klient agenta. Laczy sie z backendem przez Unix socket i przekazuje wiadomosci z Telegrama do agenta. Odpowiedzi sa formatowane w HTML i wysylane z powrotem do uzytkownika.

Obsluguje potwierdzenia przez przyciski inline (TAK / NIE), wysyla diagramy jako zdjecia, pokazuje postep
workerow w jednej, aktualizowanej wiadomosci i -- co najwazniejsze -- **sam pisze, gdy cos sie dzieje**:
alerty czuwania i raporty rutyn przychodza bez pytania, z przyciskiem *Zbadaj*.

## Alerty

Bot utrzymuje stale polaczenie z backendem (`{"command": "subscribe"}`, ponawiane po restarcie) i przesyla
zdarzenia wszystkim uzytkownikom z `TELEGRAM_ALLOWED_USER_IDS`:

- **OSTRZEZENIE / KRYTYCZNY** -- dysk, RAM, obciazenie, kontener w petli restartow albo unhealthy, zatrzymany
  kontener, nowy publiczny port. Przycisk **Zbadaj** prosi agenta o diagnoze (tylko odczyty) i propozycje naprawy.
- **ROZWIAZANE** -- problem minal.
- **Rutyna X -- OK/PROBLEM** -- raport z zadania wedlug harmonogramu.
- **Raport** -- codziennie o `DIGEST_TIME` (domyslnie 7:00): stan, zmiany od wczoraj, certyfikaty, backupy,
  aktualizacje i wykres obciazenia. Skladany bez LLM.
- **Zmiana bezpieczenstwa** -- nowe konto (uid 0 = krytyczny), nowy klucz w `authorized_keys`, nowy program SUID,
  zmiana sudoers/sshd/PAM/`ld.so.preload`, ktorej nie zrobil Pipe. Sprawdzane co 2 minuty.
- **Logowania SSH** -- seria nieudanych logowan (`WATCH_SSH_FAILURES` w 10 min), udane logowanie haslem z adresu,
  ktory zgadywal hasla (krytyczny), logowanie z nowego adresu.
- **Zbadalem alert** -- raport workera dla alertu z Alertmanagera, Grafany, Uptime Kuma albo GitHuba (webhooki).
- Alert, ktory sie powtarza, ma linie **Poprzednio:** -- co ustalono i co pomoglo ostatnim razem.
- **ZGODA** -- zewnetrzny agent (np. Claude Code przez MCP) chce wykonac zmiane: komenda doslownie, plan
  bezpiecznika i przyciski *Zatwierdz* / *Odrzuc* (tylko administratorzy). Wygasa po 30 minutach.
- **Powitanie** -- raz, po instalacji: mapa serwera, ocena bezpieczenstwa i to, czego Pipe pilnuje.
- Alerty certyfikatow (wygasa za 14 dni / nieprawidlowy), stron (nie odpowiadaja dwa razy z rzedu), DNS
  i backupow (najnowszy plik starszy niz 26 h) -- dla domen i katalogow, ktore Pipe znalazl sam.

`TELEGRAM_ALERTS=0` w `clients/telegram/.env` wylacza przesylanie. Progi: `docs/features.md#czuwanie`.

## Gdy powiadomienia nie przychodza

Alerty, raporty i przypomnienia bot dostaje stalym polaczeniem z backendem (subskrypcja zdarzen). Gdy go nie ma,
`/przypomnienia` i `/alerty` pokazuja ostrzezenie z powodem, a przypomnienie ma stan *czeka na odbior* i dotrze po
polaczeniu. Najczestsze przyczyny:

- `AGENT_TOKEN` w `clients/telegram/.env` rozni sie od tego w `backend/.env` -- w logu bota:
  `Kanal zdarzen nieaktywny: backend odrzucil subskrypcje`
- backend nie dziala albo socket (`AGENT_SOCKET`) nie jest wspolny dla obu procesow
- dziala stary proces bota (np. druga instancja spoza Dockera)

Logi: `docker compose logs telegram | grep -i "kanal\|subskrypcja"` -- prawidlowo: `Subskrypcja alertow aktywna.`
`TELEGRAM_ALERTS=0` wycisza tylko alerty czuwania i raporty; przypomnienia i prosby o zgode przychodza zawsze.

## Wiadomosci glosowe

Nagraj wiadomosc glosowa -- bot odpisze *Uslyszalem: ...* i przekaze tekst agentowi. Przy providerze `gemini`
nagranie przepisuje sam model, przy `openai` albo `groq` -- ich Whisper; dziala od razu. Przy innych ustaw w
`backend/.env` `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` (np. darmowy klucz Groq albo lokalny faster-whisper).

## Zdjecia i pliki

Wyslij zrzut ekranu, log albo plik konfiguracyjny; podpis to pytanie (*"czemu nginx tego nie przyjmuje?"*).
Kilka zdjec wyslanych naraz (album) trafia do agenta jako jedna wiadomosc. Limit 10 MB na plik. Obrazy model
oglada, tekst czyta (po ukryciu sekretow), inne pliki Pipe zapisuje na serwerze i podaje agentowi sciezke.
Viewer moze wysylac obrazy i tekst, ale nie pliki do zapisania. Szczegoly: `docs/features.md`, *Zalaczniki*.

## Role

- `TELEGRAM_ALLOWED_USER_IDS` -- administratorzy: wszystko, lacznie z TAK/NIE i `/cofnij`.
- `TELEGRAM_VIEWER_IDS` -- tylko odczyt: rozmowa, diagnoza, raporty, wykresy i alerty; bez przyciskow TAK/NIE
  i `/cofnij`. Wymaga `AGENT_VIEWER_TOKEN` w `clients/telegram/.env` i tego samego w `backend/.env`. Backend
  egzekwuje role sam -- nawet gdyby bot sie pomylil, zmiana z tokenem viewera nie zostanie wykonana.

## Diagramy

`/mapa` albo *"pokaz architekture"* -- diagram przychodzi jako zdjecie. Diagram wiekszy niz limit zdjec
Telegrama (10 MB, suma bokow 10000 px) przychodzi jako plik w pelnej rozdzielczosci.

## Formatowanie odpowiedzi

Bot uzywa trybu HTML Telegrama (nie MarkdownV2). Powod: MarkdownV2 wymaga escapowania 18 znakow specjalnych, co jest ekstremalnie podatne na bledy przy dynamicznej tresci (np. nazwy kontenerow z podkreslnikami). HTML wymaga escapowania tylko 3 znakow (<, >, &), co jest znacznie prostsze.

Obslugiwane tagi HTML:
- `<b>pogrubienie</b>` -- dla kluczowych danych
- `<i>kursywa</i>` -- dla nazw plikow
- `<code>kod inline</code>` -- dla wartosci i polecen
- `<pre>blok kodu</pre>` -- dla outputu komend
- `<u>podkreslenie</u>` -- jesli potrzebne

Bot automatycznie konwertuje resztki Markdowna (jesli LLM je wygeneruje) na odpowiadajace tagi HTML.

Tekst w backtickach -- w tym komendy w potwierdzeniach -- jest pokazywany doslownie, bez konwersji Markdowna. Przed kliknieciem TAK widzisz wiec dokladnie te komende, ktora zostanie wykonana (lacznie z `*`, `_`, `\\`, `<`, `&` i backtickami). Znaki `<`, `>` i `&` poza tagami sa escapowane automatycznie, wiec Telegram nie odrzuca wiadomosci. Kod formatowania: `clients/telegram/tg_format.py`.

### Raporty i zestawienia

- Raporty pisane przez workery (rutyny, badanie alertu, zadanie jednorazowe) przychodza jako zwykla wiadomosc:
  status w naglowku, sekcje `USTALENIA:` / `PROPOZYCJE:` pogrubione, listy z wypunktowaniem, komendy w backtickach
  doslownie. Tagi HTML z raportu sa pokazywane jako tekst -- raport powstaje bez nadzoru, wiec nie moze wstawic
  klikalnego linku.
- Zestawienia z backendu (`/zmiany`, `/zdrowie`, `/koszt`, `/dziennik`, `/przypomnienia`, `/incydenty`, wynik
  `/cofnij`) to naglowki i listy; identyfikatory (`#ab12cd`) mozna skopiowac dotknieciem.
- Blok kodu zostaje tam, gdzie tresc jest kodem albo logiem: `/historia`, roznice plikow, wynik komendy po zgodzie.

## Wymagania

- Python 3.11+
- Dzialajacy backend (`docker-compose up -d` w `backend/`)
- Dostep do Unix socket `/tmp/vps-agent.sock`
- Token bota Telegram (z @BotFather: https://t.me/botfather)

## Konfiguracja

### 1. Utworz bota przez @BotFather

```
/newbot
```

Skopiuj token bota.

### 2. Znajdz swoje user ID

Wyslij `/start` do @userinfobot (https://t.me/userinfobot) -- zwroci Twoje ID numeryczne.

### 3. Uzupelnij .env

```bash
cp .env.example .env
```

Edytuj `.env`:
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCxyz...
TELEGRAM_ALLOWED_USER_IDS=123456789
AGENT_SOCKET=/tmp/vps-agent.sock

# Musi byc identyczny z AGENT_TOKEN w backend/.env.
# Zostaw pusty, jesli backend nie wymaga tokenu.
AGENT_TOKEN=

# Jezyk bota: pl (domyslnie) albo en -- tak samo jak PIPE_LANG w backend/.env.
# PIPE_LANG=en
```

Mozesz dodac wiele ID oddzielonych przecinkami: `123456789,987654321`

## Uruchomienie

Bot uruchamia sie automatycznie razem z backendem przez `docker-compose up -d`.

Reczne uruchomienie:
```bash
pip install -r requirements.txt
python bot.py
```

## Dostepne komendy

Pelna lista ponizej (sekcja *Komendy*). Mozesz tez pisac bezposrednio, np.:
- "ile mam wolnego miejsca na dysku?"
- "pokaz architekture serwera" / "narysuj, jak dziala deploy"
- "dlaczego sklep dziala wolno?"
- "sprawdz dyski na wszystkich serwerach" (workery)
- "codziennie o 7 sprawdzaj backupy" (rutyna)
- "odpowiadaj krocej" (VIBE)

## Bezpieczenstwo

Bot milczy dla uzytkownikow spoza whitelisty -- nie odpowiada zadna wiadomoscia. Dzieki temu nie ujawnia swojego istnienia nieautoryzowanym osobom.

## Komendy

Przy `PIPE_LANG=en` menu `/` pokazuje angielskie nazwy (`/report`, `/changes`, `/undo`...). Polskie i angielskie
nazwy dzialaja w obu jezykach.

| Komenda | Dzialanie |
|---|---|
| `/status` | Szybki przeglad obciazenia serwera |
| `/aktualizuj [sprawdz]` | Aktualizacja samego Pipe (potwierdzenie przyciskami); `sprawdz` tylko pokazuje wersje |
| `/raport` | Poranny raport na zadanie: stan, alerty, zmiany od wczoraj, certyfikaty, backupy, aktualizacje, koszt LLM + wykres |
| `/zmiany [24h\|3d]` | Co sie zmienilo na serwerze: pakiety, obrazy kontenerow, porty, cron, konta, klucze SSH, konfiguracje |
| `/wykres [load\|ram\|dysk] [24h\|7d]` | Wykres z historii czuwania jako zdjecie |
| `/zdrowie` | Certyfikaty TLS, odpowiedz stron, DNS i swiezosc backupow |
| `/audyt` | Ocena bezpieczenstwa hosta 0-100 z gotowymi poprawkami (napisz *"napraw 1"*) |
| `/incydenty` | Pamiec incydentow: co sie zdarzalo, co ustalono, co pomoglo |
| `/zgody` | Operacje zewnetrznych agentow (MCP) czekajace na zgode; przyciski *Zatwierdz*/*Odrzuc* dla administratorow — z planem bezpiecznika |
| `/mcp` | Serwery MCP, z ktorych korzysta Pipe, i ich stan |
| `/dziennik` | Zatwierdzone zmiany z kopiami (co, kiedy, czy da sie cofnac) |
| `/cofnij [id]` | Cofa ostatnia (albo wskazana) zmiane: podglad roznic i komend odwrotnych, potem przycisk *Cofnij*. Dziala bez LLM |
| `/koszt` | Zuzycie tokenow LLM dzis i w ostatnich dniach (z kosztem, gdy ceny sa w `.env`) |
| `/jezyk [pl\|en]` | Jezyk Pipe (`/language`): przelacza instrukcje agenta, raporty, komunikaty bota i menu `/` -- bez restartu, wspolnie z CLI i `pipe web`; tylko administrator |
| `/providerzy [id [model]]` | Providerzy LLM (`/providers`): przyciski providerow z kluczem (aktywny -- zeby zmienic model), po wyborze lista modeli providera jako przyciski ze stronami; obecny zaznaczony, "Zostaw ..." go zostawia. `/providerzy groq llama` -- model po fragmencie nazwy, `/providerzy model` -- model obecnego providera. Kluczy API bot nie przyjmuje (zostalyby w historii czatu) -- dodasz je w `pipe web` albo w CLI. Tylko administrator |
| `/yolo [on\|off]` | Tryb YOLO: zmiany w tej rozmowie wykonuja sie bez przyciskow TAK/NIE (bezpiecznik, dziennik i `/cofnij` dzialaja dalej; zakazane -- odrzucane). Domyslnie wylaczony, tylko administrator, zapominany przy restarcie backendu |
| `/mapa [tytul]` | Diagram infrastruktury jako zdjecie (bez LLM -- szybko i za darmo) |
| `/server` | Pokazuje SERVER.md. Gdy go nie ma -- agent bada serwer i tworzy plik |
| `/server aktualizuj` | Agent bada serwer ponownie i aktualizuje SERVER.md i DIRECTORY |
| `/katalogi` | Mapa repozytoriow i katalogow (DIRECTORY) |
| `/skille` | Lista zapisanych skilli z ich komendami |
| `/alerty` | Aktywne alerty czuwania i ostatnie zdarzenia |
| `/rutyny` | Zadania wykonywane wedlug harmonogramu |
| `/przypomnienia [anuluj <id>]` | Jednorazowe przypomnienia -- przychodza same o czasie do osoby, ktora je ustawila |
| `/cele` | Zdalne serwery, kontenery i klastry |
| `/vibe` | Co agent wie o Twoim stylu rozmowy; `/vibe reset` czysci |
| `/<skill>` | Uruchamia skill, np. `/odnow_certyfikat`. Mozna dopisac wskazowki: `/odnow_certyfikat tylko dla example.com` |
| `/historia` | Ostatnie wpisy z audit logu (bez LLM) |
| `/pomoc` | Lista komend |

Menu podpowiedzi po wpisaniu `/` bot ustawia sam przy starcie i odswieza po kazdej wiadomosci -- nowy skill
pojawia sie w menu od razu. Menu jest ustawiane osobno dla czatu kazdego uzytkownika z
`TELEGRAM_ALLOWED_USER_IDS`, a nie globalnie, wiec osoby spoza listy nie widza nazw ani opisow skilli.
Jesli nie widzisz menu, napisz do bota cokolwiek (Telegram pozwala ustawic menu dopiero po rozpoczeciu czatu).

Na nieznana komende bot odpowiada podpowiedzia zamiast milczec.
