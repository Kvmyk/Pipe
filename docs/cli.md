# CLI -- Pipe

Pipe v0.46.0

Interaktywny terminal do zarzadzania serwerem VPS przez agenta AI.

Dziala na Twoim laptopie -- laczy sie zdalnie z backendem uruchomionym na serwerze przez automatyczny tunel SSH.

## Jak to dziala

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

CLI automatycznie zestawia tunel SSH -- nie musisz nic robic recznie.

Po polaczeniu CLI od razu pokazuje stan serwera jednym wierszem (bez modelu) i liczbe nowych wiadomosci od Pipe.
Gdy czekasz przy prompcie, wiadomosci, ktore Pipe napisal sam (alerty, ustalenia, podpowiedzi), pojawiaja sie nad nim
jako ramki -- te same, o ktorych Pipe powiadamia na Telegramie (`/powiadomienia`). Akcje wiadomosci wybierasz w
`/nowosci`, wpisujac numer wiadomosci i litere akcji, np. `1a`.

## Wymagania

- Python 3.11+
- `ssh` dostepny w terminalu (`which ssh`)
- Konto SSH na serwerze
- Dzialajacy backend na serwerze (`docker-compose up -d` w katalogu `backend/`)

## Instalacja

```bash
pip install -r requirements.txt
```

## Uzycie

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

## Jesli masz juz wlasny tunel SSH

```bash
# Recznie zestawiasz tunel w tle
ssh -N -L 7379:127.0.0.1:7379 root@serwer.example.com &

# CLI z wylaczonym auto-tunnel
python cli.py --no-tunnel --local-port 7379
```

## Pipe w Kubernetesie

Zamiast tunelu SSH CLI zestawia `kubectl port-forward svc/pipe` (potrzebny `kubectl` i dostep do klastra):

```bash
pipe --kube pipe                          # namespace, w ktorym dziala Pipe
pipe --kube pipe --kube-context prod      # inny kontekst kubeconfig
```

## Interfejs w przegladarce (`pipe web`)

```bash
pipe web                  # zestawia tunel jak zwykle i otwiera http://127.0.0.1:7400/?k=...
pipe web --no-browser     # tylko wypisz adres
pipe web --web-port 8080  # inny port (albo zmienna PIPE_WEB_PORT)
pipe web --lang en
pipe web --setup          # kreator od nowa: gdzie ma dzialac Pipe
```

Bez `--host` i bez zapisanego polaczenia `pipe web` zaczyna od kreatora: co chcesz robic (widok prosty albo
techniczny) i gdzie ma dzialac Pipe -- na tym komputerze (Docker) albo na Twoim serwerze (adres i haslo albo klucz SSH;
strona sama klonuje repozytorium i uruchamia instalator). Haslo zostaje na tym komputerze. Polaczenie trafia do
`~/.pipe/app.json` i od tej pory `pipe web` oraz `pipe` bez `--host` lacza sie same. Opis: `docs/features.md`
(Widoki).

Ten sam proces co CLI: tunel SSH (albo `--kube`, `--no-tunnel`), a zamiast REPL-a -- lokalna strona. Token backendu
zostaje w procesie `pipe web`; przegladarka dostaje tylko jednorazowy klucz w adresie. Strona dziala wylacznie na
`127.0.0.1` i odrzuca zadania z obcym naglowkiem `Host` albo `Origin`.

Na stronie: rozmowa i trzy zakladki panelu bocznego. *Pulpit* odpowiada bez modelu, czy wszystko dziala (pierscienie
procesora, pamieci i dysku, skrzynka *Od Pipe* z przyciskami akcji, karty projektow z warsztatu, zuzycie modelu dzis),
*Schemat* pokazuje serwer na zywo (trzy poziomy: serwery, wnetrze serwera, projekt compose; przycisk *Sledze agenta*),
*Zmiany* -- roznice i cofanie. Skille uruchamiasz z listy `/`. Spinacz przy polu rozmowy (albo przeciagniecie pliku,
albo wklejenie zrzutu ekranu) dolacza pliki do wiadomosci, a mikrofon nagrywa glosowke. Po wpisaniu `/` w polu rozmowy rozwija sie lista komend
i skilli -- dzialaja te same komendy co ponizej. Przy pierwszym wejsciu strona pyta o providera LLM (klucz API
i model), a przelacznik nad polem rozmowy pozwala skakac miedzy providerami, do ktorych jest klucz (`/provider`). W odroznieniu od REPL-a strona odbiera alerty i przypomnienia od razu. Szczegoly: `clients/webui/README.md`.

## Most MCP (`--mcp`)

`pipe --mcp --host root@serwer` zamienia CLI w serwer MCP (stdio) dla Claude Code, Cursora i innych agentow:
narzedzia Pipe przez ten sam tunel SSH i token. Na stdout idzie tylko protokol MCP; tunel ma wlasny wolny port
i nie pyta o haslo (`BatchMode` -- potrzebny klucz SSH).

Gotowa konfiguracja dla konkretnej aplikacji -- do wklejenia, z instrukcja, gdzie:

```bash
pipe connect claude --host root@serwer          # polecenie claude mcp add
pipe connect claude-desktop --host root@serwer  # claude_desktop_config.json
pipe connect cursor --host root@serwer          # ~/.cursor/mcp.json
pipe connect codex --host root@serwer           # ~/.codex/config.toml
pipe connect hermes --host root@serwer          # mcp_servers w config.yaml Hermes Agent
pipe connect openclaw --host root@serwer        # openclaw mcp add
```

Te same opcje co zwykle CLI (`--ssh-port`, `--key`, `--kube`). Token trafia zawsze do zmiennej `AGENT_TOKEN`,
nigdy do argumentow procesu; bez `--token` w miejscu tokenu jest `<TOKEN>` i podpowiedz, jak utworzyc osobny token
dla tej aplikacji (`python3 -m backend.tokens add hermes --role admin`). Recznie, np. w Claude Code:

```json
{"mcpServers": {"pipe": {"command": "pipe", "args": ["--mcp", "--host", "root@serwer"],
                         "env": {"AGENT_TOKEN": "<token z python3 -m backend.tokens add claude-code>"}}}}
```

## Diagramy

Diagram (np. `/mapa` albo *"narysuj architekture"*) CLI zapisuje jako PNG w `~/.pipe/diagrams/`
(`PIPE_DIAGRAMS_DIR`) i pokazuje jego podglad ASCII, jesli miesci sie w terminalu. `--open` otwiera PNG
w domyslnej przegladarce obrazow, `/mermaid` wypisuje kod ostatniego diagramu (np. do README).

Postep workerow (`› web-1 $ uptime`) jest wypisywany na biezaco, zanim agent odpowie.

## Zmienne srodowiskowe

| Zmienna | Opis | Domyslnie |
|---------|------|-----------|
| `VPS_HOST` | Adres serwera (`user@host`) | -- |
| `AGENT_TOKEN` | Token autoryzacji backendu (rownowazny `--token`); wymagany tylko gdy backend ma ustawione `AGENT_TOKEN` | -- |
| `VPS_SSH_PORT` | Port SSH | `22` |
| `VPS_SSH_KEY` | Sciezka do klucza prywatnego | domyslny klucz |
| `VPS_LOCAL_PORT` | Lokalny port tunelu; gdy jest zajety (drugie otwarte CLI, stary tunel), CLI bierze inny wolny | `7379` |
| `VPS_REMOTE_SOCKET` | Socket na serwerze | `/tmp/vps-agent.sock` |
| `PIPE_KUBE_NAMESPACE` | Jak `--kube` | -- |
| `PIPE_KUBE_CONTEXT` | Jak `--kube-context` | biezacy kontekst |
| `PIPE_DIAGRAMS_DIR` | Gdzie zapisywac diagramy | `~/.pipe/diagrams` |
| `PIPE_LANG` | Jezyk CLI: `pl` albo `en` (rownowazne `--lang`) | `pl` |

## Przykladowe komendy w CLI

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

Wpisz `exit` lub nacisnij `Ctrl+C` aby wyjsc.

## Komendy

Po wpisaniu `/` pod promptem pojawia sie lista komend i skilli z opisami; zaweza sie w trakcie pisania
(pasuje nazwa polska, angielska i alias). `Tab` wstawia pierwsza pozycje, kolejne `Tab` / strzalki wybieraja
nastepne, strzalka w gore przywoluje wczesniejsze wpisy. Wymaga `prompt_toolkit` (jest w `requirements.txt`) w tym samym
Pythonie / venv, z ktorego startuje `pipe` -- CLI przy starcie podaje gotowe polecenie instalacji, gdy go brakuje;
bez niego CLI dziala jak dotad, bez podpowiedzi.

Kazda komenda ma angielski alias, ktory dziala w obu jezykach: `/report`, `/changes`, `/chart`, `/health`,
`/audit`, `/news`, `/projects`, `/cleanup`, `/notifications`, `/map`, `/directory`, `/skills`, `/alerts`, `/incidents`, `/routines`, `/targets`, `/journal`, `/undo`, `/patch`,
`/approvals`, `/cost`, `/history`, `/providers`, `/language`, `/help`. `pipe --lang en` przelacza komunikaty CLI na angielski
tylko w tym uruchomieniu; `/jezyk en` (`/language pl`) zmienia jezyk calego Pipe -- instrukcje agenta, raporty,
komunikaty -- bez restartu, takze w `pipe web` i na Telegramie. Wybor zostaje po restarcie i CLI przejmuje go
przy polaczeniu (chyba ze podano `--lang`).

| Komenda | Dzialanie |
|---|---|
| `/status` | Stan serwera od razu, bez AI: naglowek zwyklymi slowami, paski procesora, pamieci i dysku, nowe wiadomosci, projekty |
| `/nowosci` | Skrzynka: co Pipe napisal sam, z akcjami -- wpisz numer i litere, np. `1a` (`/news`) |
| `/projekty` | Projekty w warsztacie: stan i adres (`/projects`). Nowy projekt: napisz np. *"sklonuj https://github.com/... i uruchom"* |
| `/web` | Pipe w przegladarce z telefonu i komputera pod adresem HTTPS: `/web wlacz [port] [domena]`, `/web wylacz`, `/web wpusc <kod>` (wpuszcza urzadzenie), `/web odwolaj <nr>`; bez argumentow stan i urzadzenia (`docs/features.md`) |
| `/sprzataj` | Zwalnia miejsce bez AI: obrazy Dockera bez tagu, stary cache budowania, pobrane paczki i cache npm/pip; tylko administrator (`/cleanup`) |
| `/powiadomienia [wszystko\|wazne\|krytyczne] [cisza 22-7\|cisza off] [dziennie 3]` | Kiedy Pipe ma powiadamiac; bez argumentow pokazuje obecne ustawienia (`/notifications`) |
| `/aktualizuj [sprawdz]` | Aktualizacja samego Pipe na serwerze (po potwierdzeniu); `sprawdz` tylko pokazuje wersje; po restarcie backendu CLI laczy sie ponownie samo (do 3 min), bez uruchamiania od nowa |
| `/raport` | Poranny raport na zadanie: stan, alerty, zmiany od wczoraj, certyfikaty, backupy, aktualizacje, koszt LLM + wykres |
| `/zmiany [24h\|3d]` | Co sie zmienilo na serwerze: pakiety, obrazy kontenerow, porty, cron, konta, klucze SSH, konfiguracje |
| `/wykres [load\|ram\|dysk] [24h\|7d]` | Wykres z historii czuwania (PNG w `~/.pipe/diagrams/`) |
| `/zdrowie` | Certyfikaty TLS, odpowiedz stron, DNS i swiezosc backupow (bez podpowiedzi i bez `/pomoc` -- problemy pokazuje `/status`) |
| `/audyt` | Ocena bezpieczenstwa hosta 0-100 z gotowymi poprawkami (napisz *"napraw 1"*) |
| `/incydenty` | Pamiec incydentow: co sie zdarzalo, co ustalono, co pomoglo |
| `/zgody` | Operacje zewnetrznych agentow (MCP) czekajace na zgode — z planem bezpiecznika |
| `/mcp [dodaj <dodatek>\|<nazwa> <npx ...\|uvx ...\|URL> \| usun <nazwa> \| odswiez]` | Bez argumentow okno w ramce (strzalki, Enter, Esc): podlaczone serwery ze szczegolami i gotowe dodatki (Google, Outlook, Microsoft 365, GitHub); klucze wpisuje sie tu ukrytym polem. Serwery dzialaja w izolowanym kontenerze. Zmiany tylko administrator |
| `/dziennik` | Zatwierdzone zmiany z kopiami (co, kiedy, czy da sie cofnac) |
| `/cofnij [id]` | Cofa ostatnia (albo wskazana) zmiane: podglad roznic i komend odwrotnych, potem pytanie TAK/NIE. Dziala bez LLM |
| `/podatnosci` | Znane podatnosci w pakietach, obrazach kontenerow, na zdalnych serwerach i w aplikacjach, kazda z krotkim wyjasnieniem w jezyku Pipe |
| `/zalataj [obrazy\|serwer\|aplikacje\|serwery\|<cel>]` | Lata od razu, bez dodatkowego pytania: pakiety tego serwera, zaleznosci aplikacji (podbite w plikach zaleznosci, projekt compose przebudowany), obrazy kontenerow z compose (nowszy obraz albo poprawki zainstalowane w obrazie) i zdalne serwery, potem sprzata stare obrazy i cache. Kroki widac na zywo, na koncu co zalatane, co bez zmian i dlaczego. Dziala bez LLM |
| `/koszt` | Zuzycie tokenow LLM dzis i w ostatnich dniach (z kosztem, gdy ceny sa w `.env`) |
| `/widok [prosty\|techniczny\|opis]` | Widok Pipe (`/view`), wspolny dla CLI, weba i Telegrama: prosty chowa komendy serwera w podpowiedziach i `/pomoc`, agent pisze zwyklymi slowami. Bez argumentu pokazuje obecny; opis zwyklymi slowami tez dziala. Przy pierwszym polaczeniu CLI pyta o niego samo. Tylko administrator |
| `/jezyk [pl\|en]` | Jezyk Pipe: bez argumentu pokazuje obecny, z argumentem przelacza caly agent (wspolny dla CLI, weba i Telegrama; tylko administrator) |
| `/providerzy [id [model]]` | Providerzy LLM (`/providers`): lista, potem wybor providera i modelu z listy, ktora zwraca provider (obecny zaznaczony, Enter go zostawia; fragment nazwy zaweza liste). Klucz API wpisywany bez echa. `/providerzy groq` -- od razu do modeli Groq, `/providerzy groq llama` -- model po fragmencie nazwy, `/providerzy model` -- tylko zmiana modelu obecnego providera, `/providerzy zapomnij <id>` -- usuwa klucz dodany z interfejsu. Wybor dotyczy calego agenta; zmiana tylko dla administratora |
| `/plik <sciezka> [pytanie]` | Wyslij agentowi plik z tego komputera (log, konfiguracja, zrzut ekranu; do 10 MB). W zwyklej wiadomosci wystarczy `@sciezka`, np. `co tu nie gra? @nginx.conf`; Tab podpowiada sciezki. Alias: `/file` |
| `/yolo [on\|off]` | Tryb YOLO: zmiany w tej rozmowie wykonuja sie bez pytania o TAK (bezpiecznik, dziennik i `/cofnij` dzialaja dalej, operacje zakazane sa nadal odrzucane). Domyslnie wylaczony, tylko administrator, zapominany przy restarcie backendu; prompt pokazuje wtedy czerwone `YOLO` |
| `/mapa [tytul]` | Diagram infrastruktury (bez LLM) |
| `/mermaid` | Kod Mermaid ostatniego diagramu |
| `/server` | Pokazuje SERVER.md; gdy go nie ma -- agent bada serwer i tworzy plik |
| `/server aktualizuj` | Agent bada serwer ponownie i aktualizuje SERVER.md i DIRECTORY |
| `/katalogi` | Mapa repozytoriow i katalogow (DIRECTORY) |
| `/skille` | Lista zapisanych skilli z ich komendami |
| `/pobierz skill\|plugin <wlasciciel/repo\|URL> [filtr]` | Skille i pluginy z repozytorium git: podglad, potem pytanie TAK/NIE (`/download`); tylko administrator |
| `/alerty` | Aktywne alerty czuwania (bez podpowiedzi i bez `/pomoc` -- nowe pokazuja sie same nad promptem i w `/nowosci`) |
| `/rutyny` | Zadania wykonywane wedlug harmonogramu |
| `/przypomnienia [anuluj <id>]` | Jednorazowe przypomnienia; CLI pokazuje je samo nad promptem (sprawdza co 10 s), takze wynik `/aktualizuj`; ustawione z CLI ida rowniez na Telegram i do `pipe web` |
| `/cele` | Zdalne serwery, kontenery i klastry |
| `/nowa` | Nowa rozmowa od czystej karty; notatki o serwerze, katalogi, skille, styl i alerty zostaja |
| `/vibe` | Notatka o Twoim stylu rozmowy; `/vibe reset` czysci |
| `/historia` | Ostatnie wpisy audit logu (bez podpowiedzi i bez `/pomoc` -- zmiany pokazuje `/dziennik`) |
| `/<skill>` | Uruchamia skill, np. `/odnow_certyfikat` albo `/odnow-certyfikat`, opcjonalnie z wskazowkami |
| `/pomoc` | Lista komend |
| `/exit` | Wyjscie |

Tekst zaczynajacy sie od `/`, ktory nie jest komenda (np. `/var/log jest pelny?`), trafia do agenta jak zwykla wiadomosc.

## Uruchomienie bezposrednio na serwerze

Po zalogowaniu przez SSH na serwer mozesz rozmawiac z agentem bez tunelu -- backend wystawia port `7379`
na `127.0.0.1` serwera:

```bash
pip install -r clients/cli/requirements.txt   # jednorazowo
python3 clients/cli/cli.py --no-tunnel        # + --token ..., jesli ustawiles AGENT_TOKEN
```

`install.sh` buduje skrot z tunelem SSH, wiec na samym serwerze wygodniej dodac alias recznie, np.
`alias pipe='python3 ~/Pipe/clients/cli/cli.py --no-tunnel'`. Pamiec agenta (SERVER.md, skille) jest wspolna
dla wszystkich klientow -- to, co agent zapisze w Telegramie, widac w terminalu i odwrotnie. Wyjatkiem jest
VIBE: notatka o stylu jest osobna dla kazdego uzytkownika (`cli:<login>`, `telegram:<id>`).
