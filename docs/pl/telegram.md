# Telegram Bot -- Pipe

Pipe v0.50.1

Bot Telegram do zarządzania serwerem VPS przez agenta AI.

## Opis

Bot Telegram to klient agenta. Łączy się z backendem przez Unix socket i przekazuje wiadomości z Telegrama do agenta. Odpowiedzi są formatowane w HTML i wysyłane z powrotem do użytkownika.

Obsługuje potwierdzenia przez przyciski inline (TAK / NIE; decyzja dopisuje się do tej samej wiadomości), wysyła
diagramy jako zdjęcia, pokazuje postęp w jednej, aktualizowanej wiadomości -- każdą komendę agenta i kilka linii jej
wyniku (po zakończeniu zwija się do linii "Wykonane kroki: N", listę rozwijasz dotknięciem) i -- co najważniejsze -- **sam pisze, gdy coś się dzieje**:
alerty czuwania, podpowiedzi, ustalenia z samodzielnego badania i raporty rutyn przychodzą bez pytania, każde
z przyciskami akcji (*Zbadaj*, *Zwolnij miejsce*, *Załataj*, *Napraw*).

Pod polem wiadomości jest stała klawiatura: *Stan serwera*, *Co nowego*, *Projekty*, *Pomoc* -- najczęstsze sprawy
bez znajomości komend. Menu `/` pokazuje tylko komendy używane co dzień; pełna lista jest w `/pomoc`.

## Alerty

Bot utrzymuje stałe połączenie z backendem (`{"command": "subscribe"}`, ponawiane po restarcie) i przesyła
zdarzenia wszystkim użytkownikom z `TELEGRAM_ALLOWED_USER_IDS`:

- **OSTRZEZENIE / KRYTYCZNY** -- dysk, RAM, obciążenie, kontener w pętli restartów albo unhealthy, zatrzymany
  kontener, nowy publiczny port. Przycisk **Zbadaj** prosi agenta o diagnozę (tylko odczyty) i propozycje naprawy,
  przy pełnym dysku jest też **Zwolnij miejsce**.
- **Ustalenia** -- przy krytycznym alercie Pipe nie czeka na *Zbadaj*: worker sam sprawdza przyczynę (tylko odczyty)
  i zaraz po alercie pisze, co ustalił, z przyciskiem **Napraw** (jedno potwierdzenie; tylko administratorzy).
  `AUTO_INVESTIGATE` w `backend/.env`.
- **Podpowiedź** -- sprawa, która dopiero będzie problemem albo coś do poprawy: kiedy zapełni się dysk, ile miejsca
  da się odzyskać, kontener, który często się restartuje, serwer czekający na restart, stale rosnący RAM. Liczona
  bez AI. Przyciski: akcja (np. *Zwolnij miejsce*), **Później** (wraca za dobę), **Mniej takich** (ten rodzaj
  zostaje tylko w skrzynce).
- **ROZWIAZANE** -- problem minął.
- **Rutyna X -- OK/PROBLEM** -- raport z zadania według harmonogramu.
- **Raport** -- codziennie o `DIGEST_TIME` (domyślnie 7:00): stan, zmiany od wczoraj, certyfikaty, backupy,
  aktualizacje, podatności i wykres obciążenia. Składany bez LLM.
- **Zmiana bezpieczeństwa** -- nowe konto (uid 0 = krytyczny), nowy klucz w `authorized_keys`, nowy program SUID,
  zmiana sudoers/sshd/PAM/`ld.so.preload`, której nie zrobił Pipe. Sprawdzane co 2 minuty.
- **Logowania SSH** -- seria nieudanych logowań (`WATCH_SSH_FAILURES` w 10 min), udane logowanie hasłem z adresu,
  który zgadywał hasła (krytyczny), logowanie z nowego adresu.
- **Podatności** -- nowe pilne podatności z poprawką (aktywnie wykorzystywane, krytyczne, wysokie) w pakietach
  serwera, obrazach kontenerów i na zdalnych serwerach; w zależnościach aplikacji tylko krytyczne i aktywnie
  wykorzystywane. Przy każdej krótko, co grozi. Przycisk **Załataj** (tylko administratorzy) od razu instaluje
  poprawki tego, czego dotyczy alert, a alert mówi, czego nie załata (poprawki tylko z Ubuntu Pro, kontenery bez
  compose, kontenery samego Pipe). Znana podatność nie wraca, także po restarcie Pipe.
- **Ustalenia dla alertu z zewnątrz** -- to samo dla alertu z Alertmanagera, Grafany, Uptime Kuma albo GitHuba
  (webhooki, `WEBHOOK_INVESTIGATE`), także z przyciskiem **Napraw**.
- Alert, który się powtarza, ma linię **Poprzednio:** -- co ustalono i co pomogło ostatnim razem.
- **ZGODA** -- zewnętrzny agent (np. Claude Code przez MCP) chce wykonać zmianę: komenda dosłownie, plan
  bezpiecznika i przyciski *Zatwierdź* / *Odrzuć* (tylko administratorzy). Wygasa po 30 minutach.
- **Powitanie** -- raz, po instalacji: mapa serwera, ocena bezpieczeństwa i to, czego Pipe pilnuje.
- Alerty certyfikatów (wygasa za 14 dni / nieprawidłowy), stron (nie odpowiadają dwa razy z rzędu), DNS
  i backupów (najnowszy plik starszy niż 26 h) -- dla domen i katalogów, które Pipe znalazł sam.

Wszystko, co Pipe napisał sam, trafia też do skrzynki: `/nowosci` (przycisk *Co nowego*) pokazuje ostatnie
wiadomości z ich przyciskami. Kiedy bot ma powiadamiać, ustawiasz przyciskami w `/powiadomienia`: poziom
(wszystko / ważne / tylko krytyczne), cisza nocna (w nocy tylko sprawy krytyczne, reszta rano z raportem), limit
podpowiedzi dziennie. Wiadomość, która nie przeszła przez te ustawienia, czeka w skrzynce.

`TELEGRAM_ALERTS=0` w `clients/telegram/.env` wyłącza przesyłanie. Progi: `docs/features.md#czuwanie`.

## Gdy powiadomienia nie przychodzą

Alerty, raporty i przypomnienia bot dostaje stałym połączeniem z backendem (subskrypcja zdarzeń). Gdy go nie ma,
`/przypomnienia` i `/alerty` pokazują ostrzeżenie z powodem, a przypomnienie ma stan *czeka na odbiór* i dotrze po
połączeniu. Najczęstsze przyczyny:

- `AGENT_TOKEN` w `clients/telegram/.env` różni się od tego w `backend/.env` -- w logu bota:
  `Kanal zdarzen nieaktywny: backend odrzucil subskrypcje`
- backend nie działa albo socket (`AGENT_SOCKET`) nie jest wspólny dla obu procesów
- działa stary proces bota (np. druga instancja spoza Dockera)

Logi: `docker compose logs telegram | grep -i "kanal\|subskrypcja"` -- prawidłowo: `Subskrypcja alertow aktywna.`
`TELEGRAM_ALERTS=0` wycisza tylko alerty czuwania i raporty; przypomnienia i prośby o zgodę przychodzą zawsze.

## Wiadomości głosowe

Nagraj wiadomość głosowa -- bot odpisze *Usłyszałem: ...* i przekaże tekst agentowi. Przy providerze `gemini`
nagranie przepisuje sam model, przy `openai` albo `groq` -- ich Whisper; działa od razu. Przy innych ustaw w
`backend/.env` `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` (np. darmowy klucz Groq albo lokalny faster-whisper).

## Zdjęcia i pliki

Wyślij zrzut ekranu, log albo plik konfiguracyjny; podpis to pytanie (*"czemu nginx tego nie przyjmuje?"*).
Kilka zdjęć wysłanych naraz (album) trafia do agenta jako jedna wiadomość. Limit 10 MB na plik. Obrazy model
ogląda, tekst czyta (po ukryciu sekretów), inne pliki Pipe zapisuje na serwerze i podaje agentowi ścieżkę.
Viewer może wysyłać obrazy i tekst, ale nie pliki do zapisania. Szczegóły: `docs/features.md`, *Załączniki*.

## Role

- `TELEGRAM_ALLOWED_USER_IDS` -- administratorzy: wszystko, łącznie z TAK/NIE i `/cofnij`.
- `TELEGRAM_VIEWER_IDS` -- tylko odczyt: rozmowa, diagnoza, raporty, wykresy i alerty; bez przycisków TAK/NIE
  i `/cofnij`. Wymaga `AGENT_VIEWER_TOKEN` w `clients/telegram/.env` i tego samego w `backend/.env`. Backend
  egzekwuje role sam -- nawet gdyby bot się pomylił, zmiana z tokenem viewera nie zostanie wykonana.

## Diagramy

`/mapa` albo *"pokaż architekturę"* -- diagram przychodzi jako zdjęcie. Diagram większy niż limit zdjęć
Telegrama (10 MB, suma boków 10000 px) przychodzi jako plik w pełnej rozdzielczości.

## Formatowanie odpowiedzi

Bot używa trybu HTML Telegrama (nie MarkdownV2). Powód: MarkdownV2 wymaga escapowania 18 znaków specjalnych, co jest ekstremalnie podatne na błędy przy dynamicznej treści (np. nazwy kontenerów z podkreślnikami). HTML wymaga escapowania tylko 3 znaków (<, >, &), co jest znacznie prostsze.

Obsługiwane tagi HTML:
- `<b>pogrubienie</b>` -- dla kluczowych danych
- `<i>kursywa</i>` -- dla nazw plików
- `<code>kod inline</code>` -- dla wartości i poleceń
- `<pre>blok kodu</pre>` -- dla outputu komend
- `<u>podkreslenie</u>` -- jeśli potrzebne

Bot automatycznie konwertuje resztki Markdowna (jeśli LLM je wygeneruje) na odpowiadające tagi HTML.

Tekst w backtickach -- w tym komendy w potwierdzeniach -- jest pokazywany dosłownie, bez konwersji Markdowna. Przed kliknięciem TAK widzisz więc dokładnie tę komendę, która zostanie wykonana (łącznie z `*`, `_`, `\\`, `<`, `&` i backtickami). Znaki `<`, `>` i `&` poza tagami są escapowane automatycznie, więc Telegram nie odrzuca wiadomości. Kod formatowania: `clients/telegram/tg_format.py`.

### Raporty i zestawienia

- Raporty pisane przez workery (rutyny, badanie alertu, zadanie jednorazowe) przychodzą jako zwykła wiadomość:
  status w nagłówku, sekcje `USTALENIA:` / `PROPOZYCJE:` pogrubione, listy z wypunktowaniem, komendy w backtickach
  dosłownie. Tagi HTML z raportu są pokazywane jako tekst -- raport powstaje bez nadzoru, więc nie może wstawić
  klikalnego linku.
- Zestawienia z backendu (`/zmiany`, `/zdrowie`, `/koszt`, `/dziennik`, `/przypomnienia`, `/incydenty`, wynik
  `/cofnij`) to nagłówki i listy; identyfikatory (`#ab12cd`) można skopiować dotknięciem.
- Blok kodu zostaje tam, gdzie treść jest kodem albo logiem: `/historia`, różnice plików, wynik komendy po zgodzie.

## Wymagania

- Python 3.11+
- Działający backend (`docker-compose up -d` w `backend/`)
- Dostęp do Unix socket `/tmp/vps-agent.sock`
- Token bota Telegram (z @BotFather: https://t.me/botfather)

## Konfiguracja

### 1. Utwórz bota przez @BotFather

```
/newbot
```

Skopiuj token bota.

### 2. Znajdź swoje user ID

Wyślij `/start` do @userinfobot (https://t.me/userinfobot) -- zwróci Twoje ID numeryczne.

### 3. Uzupełnij .env

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

# Jezyk bota: en (domyslnie) albo pl -- tak samo jak PIPE_LANG w backend/.env.
PIPE_LANG=pl
```

Możesz dodać wiele ID oddzielonych przecinkami: `123456789,987654321`

## Uruchomienie

Bot uruchamia się automatycznie razem z backendem przez `docker-compose up -d`.

Ręczne uruchomienie:
```bash
pip install -r requirements.txt
python bot.py
```

## Dostępne komendy

Pełna lista poniżej (sekcja *Komendy*). Możesz też pisać bezpośrednio, np.:
- "ile mam wolnego miejsca na dysku?"
- "pokaż architekturę serwera" / "narysuj, jak działa deploy"
- "dlaczego sklep działa wolno?"
- "sprawdź dyski na wszystkich serwerach" (workery)
- "codziennie o 7 sprawdzaj backupy" (rutyna)
- "odpowiadaj krócej" (VIBE)
- "postaw mi tę aplikację: https://github.com/acme/notatnik" (warsztat)
- "napisz skrypt, który policzy zamówienia z logów" (warsztat)

## Bezpieczeństwo

Bot milczy dla użytkowników spoza whitelisty -- nie odpowiada żadna wiadomością. Dzięki temu nie ujawnia swojego istnienia nieautoryzowanym osobom.

## Komendy

Przy `PIPE_LANG=en` menu `/` pokazuje angielskie nazwy (`/report`, `/changes`, `/undo`...). Polskie i angielskie
nazwy działają w obu językach.

| Komenda | Działanie |
|---|---|
| `/status` | Stan serwera od razu, bez AI: nagłówek zwykłymi słowami (*Wszystko działa* albo co wymaga uwagi), paski procesora, pamięci i dysku, liczba nowych wiadomości i projektów |
| `/nowosci` | Skrzynka: co Pipe napisał sam (alerty, ustalenia, podpowiedzi, raporty), z przyciskami akcji (`/news`) |
| `/projekty` | Projekty w warsztacie: stan, adres, przyciski *Logi* i *Zatrzymaj*/*Uruchom* (`/projects`) |
| `/web` | Pipe w przeglądarce z telefonu i komputera: `/web wlacz` wystawia go pod adresem HTTPS, prośby o dostęp przychodzą tu z przyciskami *Wpuść* / *Tylko podgląd* / *Odrzuć* (tylko administratorzy); `/web wylacz`, `/web odwolaj <nr>` |
| `/sprzataj` | Zwalnia miejsce bez AI: obrazy Dockera bez tagu, stary cache budowania, pobrane paczki apt i cache npm/pip; mówi, ile zwolnił. Tylko administrator (`/cleanup`) |
| `/powiadomienia` | Kiedy Pipe ma powiadamiać: poziom, cisza nocna, limit podpowiedzi -- przyciskami (`/notifications`) |
| `/aktualizuj [sprawdz]` | Aktualizacja samego Pipe (potwierdzenie przyciskami); `sprawdz` tylko pokazuje wersje |
| `/raport` | Poranny raport na żądanie: stan, alerty, zmiany od wczoraj, certyfikaty, backupy, aktualizacje, koszt LLM + wykres |
| `/zmiany [24h\|3d]` | Co się zmieniło na serwerze: pakiety, obrazy kontenerów, porty, cron, konta, klucze SSH, konfiguracje |
| `/wykres [load\|ram\|dysk] [24h\|7d]` | Wykres z historii czuwania jako zdjęcie |
| `/zdrowie` | Certyfikaty TLS, odpowiedź stron, DNS i świeżość backupów (nie ma jej w menu ani w `/pomoc` -- problemy pokazuje `/status`) |
| `/audyt` | Ocena bezpieczeństwa hosta 0-100 z gotowymi poprawkami (napisz *"napraw 1"*) |
| `/incydenty` | Pamięć incydentów: co się zdarzało, co ustalono, co pomogło |
| `/zgody` | Operacje zewnętrznych agentów (MCP) czekające na zgodę; przyciski *Zatwierdź*/*Odrzuć* dla administratorów — z planem bezpiecznika |
| `/mcp [dodaj <dodatek>\|<nazwa> <npx ...\|uvx ...\|URL> \| usun <nazwa> \| odswiez]` | Podłączone serwery MCP ze stanem i gotowe dodatki (Google, Outlook, Microsoft 365, GitHub) z przyciskiem *Dodaj* (administratorzy). Kluczy nie wpisuj w czat -- okno `/mcp` w `pipe web` albo w CLI |
| `/dziennik` | Zatwierdzone zmiany z kopiami (co, kiedy, czy da się cofnąć) |
| `/cofnij [id]` | Cofa ostatnia (albo wskazana) zmianę: podgląd różnic i komend odwrotnych, potem przycisk *Cofnij*. Działa bez LLM |
| `/podatnosci` | Znane podatności w pakietach, obrazach kontenerów, na zdalnych serwerach i w aplikacjach, każda z krótkim wyjaśnieniem. Dostępne też dla viewerów |
| `/zalataj [obrazy\|serwer\|aplikacje\|serwery\|<cel>]` | Łata od razu, bez przycisków i pytań (administrator): pakiety tego serwera, zależności aplikacji (podbite w plikach zależności, projekt compose przebudowany), obrazy kontenerów i zdalne serwery, potem sprząta stare obrazy i cache. Jedna wiadomość z krokami na żywo, na końcu krótki wynik: co załatane, co bez zmian i dlaczego, ile miejsca zwolnione. Przycisk *Załataj* przy alercie o podatnościach łata to, czego alert dotyczy. Działa bez LLM |
| `/koszt` | Zużycie tokenów LLM dziś i w ostatnich dniach (z kosztem, gdy ceny są w `.env`) |
| `/widok [prosty\|techniczny\|opis]` | Widok Pipe (`/view`): bez argumentu przyciski *Prosty* / *Techniczny*. Prosty zmienia menu `/`, `/pomoc` i klawiaturę (stan, nowości, skille, pomoc), a agent pisze zwykłymi słowami; wspólny z CLI i `pipe web`. Przy pierwszym `/start` bot pyta, co chcesz robić -- przycisk albo kilka słów. Tylko administrator |
| `/jezyk [pl\|en]` | Język Pipe (`/language`): przełącza instrukcje agenta, raporty, komunikaty bota i menu `/` -- bez restartu, wspólnie z CLI i `pipe web`; tylko administrator |
| `/providerzy [id [model]]` | Providerzy LLM (`/providers`): przyciski providerów z kluczem (aktywny -- żeby zmienić model), po wyborze lista modeli providera jako przyciski ze stronami; obecny zaznaczony, "Zostaw ..." go zostawia. `/providerzy groq llama` -- model po fragmencie nazwy, `/providerzy model` -- model obecnego providera. Kluczy API bot nie przyjmuje (zostałyby w historii czatu) -- dodasz je w `pipe web` albo w CLI. Tylko administrator |
| `/yolo [on\|off]` | Tryb YOLO: zmiany w tej rozmowie wykonują się bez przycisków TAK/NIE (bezpiecznik, dziennik i `/cofnij` działają dalej; zakazane -- odrzucane). Domyślnie wyłączony, tylko administrator, zapominany przy restarcie backendu |
| `/mapa [tytul]` | Diagram infrastruktury jako zdjęcie (bez LLM -- szybko i za darmo) |
| `/server` | Pokazuje SERVER.md. Gdy go nie ma -- agent bada serwer i tworzy plik |
| `/server aktualizuj` | Agent bada serwer ponownie i aktualizuje SERVER.md i DIRECTORY |
| `/katalogi` | Mapa repozytoriów i katalogów (DIRECTORY) |
| `/skille` | Lista zapisanych skilli z ich komendami |
| `/pobierz skill\|plugin <wlasciciel/repo\|URL> [filtr]` | Skille i pluginy z repozytorium git: podgląd z przyciskami *Zainstaluj* / *Anuluj* (`/download`); tylko administrator |
| `/alerty` | Aktywne alerty czuwania i ostatnie zdarzenia (nie ma jej w menu ani w `/pomoc` -- wiadomości są w `/nowosci`) |
| `/rutyny` | Zadania wykonywane według harmonogramu |
| `/przypomnienia [anuluj <id>]` | Jednorazowe przypomnienia -- przychodzą same o czasie do osoby, która je ustawiła |
| `/cele` | Zdalne serwery, kontenery i klastry |
| `/nowa` | Nowa rozmowa od czystej karty (bot ma jedną rozmowę na użytkownika); notatki o serwerze, katalogi, skille, styl i alerty zostają |
| `/vibe` | Co agent wie o Twoim stylu rozmowy; `/vibe reset` czyści |
| `/<skill>` | Uruchamia skill, np. `/odnow_certyfikat`. Można dopisać wskazówki: `/odnow_certyfikat tylko dla example.com` |
| `/historia` | Ostatnie wpisy z audit logu, bez LLM (nie ma jej w menu ani w `/pomoc` -- zmiany pokazuje `/dziennik`) |
| `/pomoc` | Lista komend |

Menu podpowiedzi po wpisaniu `/` bot ustawia sam przy starcie i odświeża po każdej wiadomości -- nowy skill
pojawia się w menu od razu. Menu jest ustawiane osobno dla czatu każdego użytkownika z
`TELEGRAM_ALLOWED_USER_IDS`, a nie globalnie, więc osoby spoza listy nie widzą nazw ani opisów skilli.
Jeśli nie widzisz menu, napisz do bota cokolwiek (Telegram pozwala ustawić menu dopiero po rozpoczęciu czatu).

Na nieznaną komendę bot odpowiada podpowiedzią zamiast milczeć.
