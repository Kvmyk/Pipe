# CLI -- Pipe

Pipe v0.15.0

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

## Most MCP (`--mcp`)

`pipe --mcp --host root@serwer` zamienia CLI w serwer MCP (stdio) dla Claude Code, Cursora i innych agentow:
narzedzia Pipe przez ten sam tunel SSH i token. Na stdout idzie tylko protokol MCP; tunel ma wlasny wolny port
i nie pyta o haslo (`BatchMode` -- potrzebny klucz SSH). Konfiguracja np. w Claude Code:

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
| `VPS_LOCAL_PORT` | Lokalny port tunelu | `7379` |
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

Kazda komenda ma angielski alias, ktory dziala w obu jezykach: `/report`, `/changes`, `/chart`, `/health`,
`/audit`, `/map`, `/directory`, `/skills`, `/alerts`, `/incidents`, `/routines`, `/targets`, `/journal`, `/undo`,
`/approvals`, `/cost`, `/history`, `/help`. `pipe --lang en` przelacza komunikaty CLI na angielski (jezyk
odpowiedzi agenta ustawia `PIPE_LANG` w backendzie).

| Komenda | Dzialanie |
|---|---|
| `/status` | Stan serwera |
| `/raport` | Poranny raport na zadanie: stan, alerty, zmiany od wczoraj, certyfikaty, backupy, aktualizacje, koszt LLM + wykres |
| `/zmiany [24h\|3d]` | Co sie zmienilo na serwerze: pakiety, obrazy kontenerow, porty, cron, konta, klucze SSH, konfiguracje |
| `/wykres [load\|ram\|dysk] [24h\|7d]` | Wykres z historii czuwania (PNG w `~/.pipe/diagrams/`) |
| `/zdrowie` | Certyfikaty TLS, odpowiedz stron, DNS i swiezosc backupow |
| `/audyt` | Ocena bezpieczenstwa hosta 0-100 z gotowymi poprawkami (napisz *"napraw 1"*) |
| `/incydenty` | Pamiec incydentow: co sie zdarzalo, co ustalono, co pomoglo |
| `/zgody` | Operacje zewnetrznych agentow (MCP) czekajace na zgode — z planem bezpiecznika |
| `/mcp` | Serwery MCP, z ktorych korzysta Pipe, i ich stan |
| `/dziennik` | Zatwierdzone zmiany z kopiami (co, kiedy, czy da sie cofnac) |
| `/cofnij [id]` | Cofa ostatnia (albo wskazana) zmiane: podglad roznic i komend odwrotnych, potem pytanie TAK/NIE. Dziala bez LLM |
| `/koszt` | Zuzycie tokenow LLM dzis i w ostatnich dniach (z kosztem, gdy ceny sa w `.env`) |
| `/mapa [tytul]` | Diagram infrastruktury (bez LLM) |
| `/mermaid` | Kod Mermaid ostatniego diagramu |
| `/server` | Pokazuje SERVER.md; gdy go nie ma -- agent bada serwer i tworzy plik |
| `/server aktualizuj` | Agent bada serwer ponownie i aktualizuje SERVER.md i DIRECTORY |
| `/katalogi` | Mapa repozytoriow i katalogow (DIRECTORY) |
| `/skille` | Lista zapisanych skilli z ich komendami |
| `/alerty` | Aktywne alerty czuwania (same alerty przychodza na Telegram) |
| `/rutyny` | Zadania wykonywane wedlug harmonogramu |
| `/cele` | Zdalne serwery, kontenery i klastry |
| `/vibe` | Notatka o Twoim stylu rozmowy; `/vibe reset` czysci |
| `/historia` | Ostatnie wpisy audit logu |
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
