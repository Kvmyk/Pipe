# CLI -- Pipe

Pipe v0.50.1

Interaktywny terminal do zarządzania serwerem VPS przez agenta AI.

Działa na Twoim laptopie -- łączy się zdalnie z backendem uruchomionym na serwerze przez automatyczny tunel SSH.

## Jak to działa

```
Twoj laptop
    +-- cli.py
          +-- automatycznie otwiera tunel SSH ->
          |       ssh -L 7379:127.0.0.1:7379 user@serwer
          +-- laczy sie przez tunel z agentem na serwerze
                        |
              serwer -- backend/server.py
                        |
              /tmp/vps-agent.sock + 127.0.0.1:7379 (tylko localhost)
```

CLI automatycznie zestawia tunel SSH -- nie musisz nic robić ręcznie.

Po połączeniu CLI od razu pokazuje stan serwera jednym wierszem (bez modelu) i liczbę nowych wiadomości od Pipe.
Gdy czekasz przy prompcie, wiadomości, które Pipe napisał sam (alerty, ustalenia, podpowiedzi), pojawiają się nad nim
jako ramki -- te same, o których Pipe powiadamia na Telegramie (`/powiadomienia`). Akcje wiadomości wybierasz w
`/nowosci`, wpisując numer wiadomości i literę akcji, np. `1a`.

## Wymagania

- Python 3.11+
- `ssh` dostępny w terminalu (`which ssh`)
- Konto SSH na serwerze
- Działający backend na serwerze (`docker-compose up -d` w katalogu `backend/`)

## Instalacja

```bash
pip install -r requirements.txt
```

## Użycie

```bash
# Podstawowe -- podaj adres serwera
python cli.py --host root@serwer.example.com

# Niestandardowy port SSH
python cli.py --host root@1.2.3.4 --ssh-port 2222

# Klucz SSH (jesli nie masz domyslnego w ~/.ssh/)
python cli.py --host root@1.2.3.4 --key ~/.ssh/id_serwer

# Zmienna srodowiskowa zamiast flagi (wygodne do codziennego uzycia)
export VPS_HOST=root@serwer.example.com
python cli.py
```

W trakcie pracy agenta pod Twoją wiadomością pojawia się szara linia na każdy krok -- `✓ $ komenda` (albo opis,
np. *czyta /etc/hosts*) i do 4 linii wyniku pod nią; przy kodzie wyjścia innym niż 0 dopisek *kod N*, przy zmianie
wykonanej bez pytania -- *bez pytania*. W widoku prostym tych linii nie ma.

## Jeśli masz już własny tunel SSH

```bash
# Recznie zestawiasz tunel w tle
ssh -N -L 7379:127.0.0.1:7379 root@serwer.example.com &

# CLI z wylaczonym auto-tunnel
python cli.py --no-tunnel --local-port 7379
```

## Pipe w Kubernetesie

Zamiast tunelu SSH CLI zestawia `kubectl port-forward svc/pipe` (potrzebny `kubectl` i dostęp do klastra):

```bash
pipe --kube pipe                          # namespace, w ktorym dziala Pipe
pipe --kube pipe --kube-context prod      # inny kontekst kubeconfig
```

## Interfejs w przeglądarce (`pipe web`)

```bash
pipe web                  # zestawia tunel jak zwykle i otwiera http://127.0.0.1:7400/?k=...
pipe web --no-browser     # tylko wypisz adres
pipe web --web-port 8080  # inny port (albo zmienna PIPE_WEB_PORT)
pipe web --lang pl
pipe web --setup          # kreator od nowa: gdzie ma dzialac Pipe
```

Bez `--host` i bez zapisanego połączenia `pipe web` zaczyna od kreatora: co chcesz robić (widok prosty albo
techniczny) i gdzie ma działać Pipe -- na tym komputerze (Docker) albo na Twoim serwerze (adres i hasło albo klucz SSH;
strona sama klonuje repozytorium i uruchamia instalator). Hasło zostaje na tym komputerze. Połączenie trafia do
`~/.pipe/app.json` i od tej pory `pipe web` oraz `pipe` bez `--host` łącza się same. Opis: `docs/features.md`
(Widoki).

Ten sam proces co CLI: tunel SSH (albo `--kube`, `--no-tunnel`), a zamiast REPL-a -- lokalna strona. Token backendu
zostaje w procesie `pipe web`; przeglądarka dostaje tylko jednorazowy klucz w adresie. Strona działa wyłącznie na
`127.0.0.1` i odrzuca żądania z obcym nagłówkiem `Host` albo `Origin`.

Na stronie: rozmowa i trzy zakładki panelu bocznego. *Pulpit* odpowiada bez modelu, czy wszystko działa (pierścienie
procesora, pamięci i dysku, skrzynka *Od Pipe* z przyciskami akcji, karty projektów z warsztatu, zużycie modelu dziś),
*Schemat* pokazuje serwer na żywo (trzy poziomy: serwery, wnętrze serwera, projekt compose; przycisk *Śledzę agenta*),
*Zmiany* -- różnice i cofanie. Skille uruchamiasz z listy `/`. Spinacz przy polu rozmowy (albo przeciągnięcie pliku,
albo wklejenie zrzutu ekranu) dołącza pliki do wiadomości, a mikrofon nagrywa głosówkę. Po wpisaniu `/` w polu rozmowy rozwija się lista komend
i skilli -- działają te same komendy co poniżej. Przy pierwszym wejściu strona pyta o providera LLM (klucz API
i model), a przełącznik nad polem rozmowy pozwala skakać między providerami, do których jest klucz (`/provider`). W odróżnieniu od REPL-a strona odbiera alerty i przypomnienia od razu. Szczegóły: `clients/webui/README.md`.

## Most MCP (`--mcp`)

`pipe --mcp --host root@serwer` zamienia CLI w serwer MCP (stdio) dla Claude Code, Cursora i innych agentów:
narzędzia Pipe przez ten sam tunel SSH i token. Na stdout idzie tylko protokół MCP; tunel ma własny wolny port
i nie pyta o hasło (`BatchMode` -- potrzebny klucz SSH).

Gotowa konfiguracja dla konkretnej aplikacji -- do wklejenia, z instrukcją, gdzie:

```bash
pipe connect claude --host root@serwer          # polecenie claude mcp add
pipe connect claude-desktop --host root@serwer  # claude_desktop_config.json
pipe connect cursor --host root@serwer          # ~/.cursor/mcp.json
pipe connect codex --host root@serwer           # ~/.codex/config.toml
pipe connect hermes --host root@serwer          # mcp_servers w config.yaml Hermes Agent
pipe connect openclaw --host root@serwer        # openclaw mcp add
```

Te same opcje co zwykle CLI (`--ssh-port`, `--key`, `--kube`). Token trafia zawsze do zmiennej `AGENT_TOKEN`,
nigdy do argumentów procesu; bez `--token` w miejscu tokenu jest `<TOKEN>` i podpowiedź, jak utworzyć osobny token
dla tej aplikacji (`python3 -m backend.tokens add hermes --role admin`). Ręcznie, np. w Claude Code:

```json
{"mcpServers": {"pipe": {"command": "pipe", "args": ["--mcp", "--host", "root@serwer"],
                         "env": {"AGENT_TOKEN": "<token z python3 -m backend.tokens add claude-code>"}}}}
```

## Diagramy

Diagram (np. `/mapa` albo *"narysuj architekturę"*) CLI zapisuje jako PNG w `~/.pipe/diagrams/`
(`PIPE_DIAGRAMS_DIR`) i pokazuje jego podgląd ASCII, jeśli mieści się w terminalu. `--open` otwiera PNG
w domyślnej przeglądarce obrazów, `/mermaid` wypisuje kod ostatniego diagramu (np. do README).

Postęp workerów (`› web-1 $ uptime`) jest wypisywany na bieżąco, zanim agent odpowie.

## Zmienne środowiskowe

| Zmienna | Opis | Domyślnie |
|---------|------|-----------|
| `VPS_HOST` | Adres serwera (`user@host`) | -- |
| `AGENT_TOKEN` | Token autoryzacji backendu (równoważny `--token`); wymagany tylko gdy backend ma ustawione `AGENT_TOKEN` | -- |
| `VPS_SSH_PORT` | Port SSH | `22` |
| `VPS_SSH_KEY` | Ścieżka do klucza prywatnego | domyślny klucz |
| `VPS_LOCAL_PORT` | Lokalny port tunelu; gdy jest zajęty (drugie otwarte CLI, stary tunel), CLI bierze inny wolny | `7379` |
| `VPS_REMOTE_SOCKET` | Socket na serwerze | `/tmp/vps-agent.sock` |
| `PIPE_KUBE_NAMESPACE` | Jak `--kube` | -- |
| `PIPE_KUBE_CONTEXT` | Jak `--kube-context` | bieżący kontekst |
| `PIPE_DIAGRAMS_DIR` | Gdzie zapisywać diagramy | `~/.pipe/diagrams` |
| `PIPE_LANG` | Język CLI: `en` albo `pl` (równoważne `--lang`) | język serwera, inaczej `en` |

## Przykładowe komendy w CLI

```
> ile mam wolnego miejsca na dysku?
> pokaz ostatnie bledy nginx
> zrestartuj docker compose
> sprawdz czy port 80 jest otwarty
> sprawdz uzycie RAM przez procesy
> pokaz status repozytoriow git
> wyswietl otwarte porty
> pokaz architekture serwera
> narysuj, jak zapytanie trafia do sklepu
> gdzie lezy repozytorium bloga?
> dodaj serwer 10.0.0.5 jako web-2 (ssh, root) i sprawdz na nim dysk
> sprawdz aktualizacje na wszystkich serwerach
> codziennie o 7 sprawdzaj backupy, pisz tylko jak cos jest nie tak
```

Wpisz `exit` lub naciśnij `Ctrl+C` aby wyjść.

## Komendy

Po wpisaniu `/` pod promptem pojawia się lista komend i skilli z opisami; zawęża się w trakcie pisania
(pasuje nazwa polska, angielska i alias). `Tab` wstawia pierwszą pozycję, kolejne `Tab` / strzałki wybierają
następne, strzałka w górę przywołuje wcześniejsze wpisy. Wymaga `prompt_toolkit` (jest w `requirements.txt`) w tym samym
Pythonie / venv, z którego startuje `pipe` -- CLI przy starcie podaje gotowe polecenie instalacji, gdy go brakuje;
bez niego CLI działa jak dotąd, bez podpowiedzi.

Każda komenda ma angielski alias, który działa w obu językach: `/report`, `/changes`, `/chart`, `/health`,
`/audit`, `/news`, `/projects`, `/cleanup`, `/notifications`, `/map`, `/directory`, `/skills`, `/alerts`, `/incidents`, `/routines`, `/targets`, `/journal`, `/undo`, `/patch`,
`/approvals`, `/cost`, `/history`, `/providers`, `/language`, `/help`. Bez `--lang` CLI i `pipe web` mówią językiem serwera;
`pipe --lang pl` przełącza komunikaty CLI na polski tylko w tym uruchomieniu; `/jezyk pl` (`/language en`) zmienia język całego Pipe -- instrukcje agenta, raporty,
komunikaty -- bez restartu, także w `pipe web` i na Telegramie. Wybór zostaje po restarcie i CLI przejmuje go
przy połączeniu (chyba że podano `--lang`).

| Komenda | Działanie |
|---|---|
| `/status` | Stan serwera od razu, bez AI: nagłówek zwykłymi słowami, paski procesora, pamięci i dysku, nowe wiadomości, projekty |
| `/nowosci` | Skrzynka: co Pipe napisał sam, z akcjami -- wpisz numer i literę, np. `1a` (`/news`) |
| `/projekty` | Projekty w warsztacie: stan i adres (`/projects`). Nowy projekt: napisz np. *"sklonuj https://github.com/... i uruchom"* |
| `/web` | Pipe w przeglądarce z telefonu i komputera pod adresem HTTPS: `/web wlacz [port] [domena]`, `/web wylacz`, `/web wpusc <kod>` (wpuszcza urządzenie), `/web odwolaj <nr>`; bez argumentów stan i urządzenia (`docs/features.md`) |
| `/sprzataj` | Zwalnia miejsce bez AI: obrazy Dockera bez tagu, stary cache budowania, pobrane paczki i cache npm/pip; tylko administrator (`/cleanup`) |
| `/powiadomienia [wszystko\|wazne\|krytyczne] [cisza 22-7\|cisza off] [dziennie 3]` | Kiedy Pipe ma powiadamiać; bez argumentów pokazuje obecne ustawienia (`/notifications`) |
| `/aktualizuj [sprawdz]` | Aktualizacja samego Pipe na serwerze; `sprawdz` tylko pokazuje wersje; po restarcie backendu CLI łączy się ponownie samo (do 3 min), bez uruchamiania od nowa |
| `/raport` | Poranny raport na żądanie: stan, alerty, zmiany od wczoraj, certyfikaty, backupy, aktualizacje, koszt LLM + wykres |
| `/zmiany [24h\|3d]` | Co się zmieniło na serwerze: pakiety, obrazy kontenerów, porty, cron, konta, klucze SSH, konfiguracje |
| `/wykres [load\|ram\|dysk] [24h\|7d]` | Wykres z historii czuwania (PNG w `~/.pipe/diagrams/`) |
| `/zdrowie` | Certyfikaty TLS, odpowiedź stron, DNS i świeżość backupów (bez podpowiedzi i bez `/pomoc` -- problemy pokazuje `/status`) |
| `/audyt` | Ocena bezpieczeństwa hosta 0-100 z gotowymi poprawkami (napisz *"napraw 1"*) |
| `/incydenty` | Pamięć incydentów: co się zdarzało, co ustalono, co pomogło |
| `/zgody` | Operacje zewnętrznych agentów (MCP) czekające na zgodę — z planem bezpiecznika |
| `/mcp [dodaj <dodatek>\|<nazwa> <npx ...\|uvx ...\|URL> \| usun <nazwa> \| odswiez]` | Bez argumentów okno w ramce (strzałki, Enter, Esc): podłączone serwery ze szczegółami i gotowe dodatki (Google, Outlook, Microsoft 365, GitHub); klucze wpisuje się tu ukrytym polem. Serwery działają w izolowanym kontenerze. Zmiany tylko administrator |
| `/dziennik` | Zatwierdzone zmiany z kopiami (co, kiedy, czy da się cofnąć) |
| `/cofnij [id]` | Cofa ostatnia (albo wskazana) zmianę: podgląd różnic i komend odwrotnych, potem pytanie TAK/NIE. Działa bez LLM |
| `/podatnosci` | Znane podatności w pakietach, obrazach kontenerów, na zdalnych serwerach i w aplikacjach, każda z krótkim wyjaśnieniem w języku Pipe |
| `/zalataj [obrazy\|serwer\|aplikacje\|serwery\|<cel>]` | Łata od razu, bez dodatkowego pytania: pakiety tego serwera, zależności aplikacji (podbite w plikach zależności, projekt compose przebudowany), obrazy kontenerów z compose (nowszy obraz albo poprawki zainstalowane w obrazie) i zdalne serwery, potem sprząta stare obrazy i cache. Kroki widać na żywo, na końcu co załatane, co bez zmian i dlaczego. Działa bez LLM |
| `/koszt` | Zużycie tokenów LLM dziś i w ostatnich dniach (z kosztem, gdy ceny są w `.env`) |
| `/widok [prosty\|techniczny\|opis]` | Widok Pipe (`/view`), wspólny dla CLI, weba i Telegrama: prosty chowa komendy serwera w podpowiedziach i `/pomoc`, agent pisze zwykłymi słowami. Bez argumentu pokazuje obecny; opis zwykłymi słowami też działa. Przy pierwszym połączeniu CLI pyta o niego samo. Tylko administrator |
| `/jezyk [pl\|en]` | Język Pipe: bez argumentu pokazuje obecny, z argumentem przełącza cały agent (wspólny dla CLI, weba i Telegrama; tylko administrator) |
| `/providerzy [id [model]]` | Providerzy LLM (`/providers`): lista, potem wybór providera i modelu z listy, którą zwraca provider (obecny zaznaczony, Enter go zostawia; fragment nazwy zawęża listę). Klucz API wpisywany bez echa. `/providerzy groq` -- od razu do modeli Groq, `/providerzy groq llama` -- model po fragmencie nazwy, `/providerzy model` -- tylko zmiana modelu obecnego providera, `/providerzy zapomnij <id>` -- usuwa klucz dodany z interfejsu. Wybór dotyczy całego agenta; zmiana tylko dla administratora |
| `/plik <sciezka> [pytanie]` | Wyślij agentowi plik z tego komputera (log, konfiguracja, zrzut ekranu; do 10 MB). W zwykłej wiadomości wystarczy `@sciezka`, np. `co tu nie gra? @nginx.conf`; Tab podpowiada ścieżki. Alias: `/file` |
| `/yolo [on\|off]` | Tryb YOLO: zmiany w tej rozmowie wykonują się bez pytania o TAK (bezpiecznik, dziennik i `/cofnij` działają dalej, operacje zakazane są nadal odrzucane). Domyślnie wyłączony, tylko administrator, zapominany przy restarcie backendu; prompt pokazuje wtedy czerwone `YOLO` |
| `/mapa [tytul]` | Diagram infrastruktury (bez LLM) |
| `/mermaid` | Kod Mermaid ostatniego diagramu |
| `/server` | Pokazuje SERVER.md; gdy go nie ma -- agent bada serwer i tworzy plik |
| `/server aktualizuj` | Agent bada serwer ponownie i aktualizuje SERVER.md i DIRECTORY |
| `/katalogi` | Mapa repozytoriów i katalogów (DIRECTORY) |
| `/skille` | Lista zapisanych skilli z ich komendami |
| `/pobierz skill\|plugin <wlasciciel/repo\|URL> [filtr]` | Skille i pluginy z repozytorium git: podgląd, potem pytanie TAK/NIE (`/download`); tylko administrator |
| `/alerty` | Aktywne alerty czuwania (bez podpowiedzi i bez `/pomoc` -- nowe pokazują się same nad promptem i w `/nowosci`) |
| `/rutyny` | Zadania wykonywane według harmonogramu |
| `/przypomnienia [anuluj <id>]` | Jednorazowe przypomnienia; CLI pokazuje je samo nad promptem (sprawdza co 10 s), także wynik `/aktualizuj`; ustawione z CLI idą również na Telegram i do `pipe web` |
| `/cele` | Zdalne serwery, kontenery i klastry |
| `/nowa` | Nowa rozmowa od czystej karty; notatki o serwerze, katalogi, skille, styl i alerty zostają |
| `/vibe` | Notatka o Twoim stylu rozmowy; `/vibe reset` czyści |
| `/historia` | Ostatnie wpisy audit logu (bez podpowiedzi i bez `/pomoc` -- zmiany pokazuje `/dziennik`) |
| `/<skill>` | Uruchamia skill, np. `/odnow_certyfikat` albo `/odnow-certyfikat`, opcjonalnie z wskazówkami |
| `/pomoc` | Lista komend |
| `/exit` | Wyjście |

Tekst zaczynający się od `/`, który nie jest komenda (np. `/var/log jest pelny?`), trafia do agenta jak zwykła wiadomość.

## Uruchomienie bezpośrednio na serwerze

Po zalogowaniu przez SSH na serwer możesz rozmawiać z agentem bez tunelu -- backend wystawia port `7379`
na `127.0.0.1` serwera:

```bash
pip install -r clients/cli/requirements.txt   # jednorazowo
python3 clients/cli/cli.py --no-tunnel        # + --token ..., jesli ustawiles AGENT_TOKEN
```

`install.sh` buduje skrót z tunelem SSH, więc na samym serwerze wygodniej dodać alias ręcznie, np.
`alias pipe='python3 ~/Pipe/clients/cli/cli.py --no-tunnel'`. Pamięć agenta (SERVER.md, skille) jest wspólna
dla wszystkich klientów -- to, co agent zapisze w Telegramie, widać w terminalu i odwrotnie. Wyjątkiem jest
VIBE: notatka o stylu jest osobna dla każdego użytkownika (`cli:<login>`, `telegram:<id>`).
