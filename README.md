# Pipe

**v0.19.1** -- Agent AI, ktory pilnuje Twoich serwerow, a nie tylko odpowiada na pytania.

**Polski** · [English](./README.en.md)

Pipe dziala na serwerze na stale: zna go (SERVER.md, mapa katalogow), czuwa nad nim i odzywa sie pierwszy,
gdy cos sie psuje. Rozmawiasz z nim z terminala (CLI przez tunel SSH) albo z telefonu (Telegram) -- po polsku
albo po angielsku (`PIPE_LANG=en`, patrz [Jezyk](#jezyk)).
Kazda komenda przechodzi przez klasyfikator bezpieczenstwa w kodzie, a nie w prompcie: odczyty wykonuja sie
od razu, zmiany czekaja na Twoje TAK, operacje destrukcyjne sa odrzucane.

---

## Czym rozni sie od Claude Code, Codex czy Hermes Agent

Tamte narzedzia to ogolni agenci do kodu albo "do wszystkiego". Pipe jest agentem **operacyjnym**:

| | Pipe |
|---|---|
| **Pisze pierwszy** | Czuwanie co 2 min sprawdza dyski, RAM, obciazenie, kontenery w petli restartow i **nowe publiczne porty**. Alert przychodzi na Telegram z przyciskiem *Zbadaj*. Bez LLM, bez kosztow. Co rano -- **raport** z wykresem. |
| **Wie, co sie zmienilo** | Co godzine migawka hosta: pakiety, obrazy kontenerow, porty, cron, konta, klucze SSH, konfiguracje. *"Co sie zmienilo od wczoraj?"* ma odpowiedz z godzina -- a przy badaniu alertu agent dostaje ja sam. |
| **Monitoring bez konfiguracji** | Domeny z nginx/Caddy/Traefik i backupy z mapy katalogow Pipe znajduje sam: pilnuje waznosci certyfikatow, odpowiedzi stron, DNS i swiezosci backupow. Nic nie definiujesz. |
| **Pokazuje, co robi** | `pipe web`: obok rozmowy schemat serwera, na ktorym na zywo widac, gdzie agent pracuje -- serwery, wnetrze serwera, projekt. Kazda zmiana ma roznice "przed -> po" i przycisk cofniecia. |
| **Widzi architekture** | `/mapa` rysuje diagram tego, co stoi na serwerze: domeny -> reverse proxy -> kontenery -> bazy, projekty compose, porty wystawione na swiat. Na Telegramie przychodzi jako obraz, w CLI jako PNG + podglad w terminalu. |
| **Zarzadza flota** | Zdalne serwery (SSH), kontenery i klastry Kubernetes to *cele*. Agent wysyla na nie **workerow** -- pod-agentow, ktorzy rownolegle badaja kazdy cel i raportuja mu, a nie Tobie. Nic nie instaluje sie po drugiej stronie. |
| **Brama MCP dla innych agentow** | Claude Code, Cursor czy wlasny agent podlaczaja sie do Pipe przez MCP (`pipe --mcp --host root@serwer`) i pracuja na serwerze przez klasyfikator: odczyty od razu, zmiany dopiero po Twojej zgodzie w Telegramie -- z kopia i `/cofnij`. Pipe sam tez korzysta z innych serwerow MCP (GitHub, Grafana...). |
| **Uczy sie na incydentach** | Rozwiazany alert zostaje w pamieci z ustaleniami i tym, co pomoglo. Gdy problem wraca, alert przychodzi z *"poprzednio: przyczyna ..., pomoglo ..."*, a agent zaczyna od sprawdzonej naprawy. Lokalnie, bez dodatkowych kosztow. |
| **Dolacza do Twojego monitoringu** | Webhooki z Alertmanagera, Grafany, Uptime Kuma i GitHuba -- alert na Telegram, a worker od razu bada przyczyne na serwerze i przysyla raport. |
| **Pilnuje bezpieczenstwa** | `/audyt` ocenia serwer (0-100): hasla w SSH, zapora, bazy wystawione na swiat (takze przez Dockera, ktory omija ufw), kontenery z `docker.sock`, konta z uid 0, aktualizacje -- kazdy punkt z gotowa poprawka. Co 2 minuty wypatruje nowych kont, kluczy SSH, programow SUID i podejrzanych logowan. |
| **Zmiany, ktore da sie cofnac** | Przed kazda zatwierdzona zmiana kopia do dziennika. `nginx -t`, `sshd -t`, `docker compose config` **przed** przeladowaniem; po zmianie weryfikacja: usluga aktywna, kontener zdrowy, strony dalej odpowiadaja. Zepsuta konfiguracja wraca sama, reszta -- `/cofnij`. |
| **Bezpieczenstwo w kodzie** | Klasyfikator fail-closed (nieznana komenda = pytanie), potwierdzenie pokazuje dokladnie to, co sie wykona, workery wykonuja tylko odczyty. Sekrety z plikow (`.env`, klucze) sa **redagowane, zanim trafia do providera LLM**. |
| **Pamieta serwer, nie repo** | `SERVER.md` (fakty o serwerze), `DIRECTORY` (gdzie leza repozytoria, aplikacje, konfiguracje, backupy), skille (procedury) i **VIBE** -- agent z czasem uczy sie, jak lubisz rozmawiac. |
| **Dziala na tanim modelu** | Dowolny endpoint zgodny z OpenAI: Gemini (darmowy tier), OpenRouter, Groq, DeepSeek, lokalna Ollama... Workery moga uzywac tanszego modelu. `/koszt` liczy tokeny, a dzienny limit pilnuje budzetu. |
| **Stawiasz go wszedzie** | Docker na VPS, natywnie z systemd, w Kubernetesie (kustomize), cloud-init dla kazdej chmury. Obraz amd64 i arm64. |

---

## Szybki start

### 1. Postaw backend na serwerze

```bash
git clone https://github.com/user/pipe && cd pipe
sudo bash scripts/install-server.sh             # Docker; kreator pyta o providera i klucz
# albo: sudo bash scripts/install-server.sh --mode native   (bez Dockera, systemd)
```

Kreator pobiera aktualna liste modeli prosto od providera i sprawdza, czy wybrany model obsluguje
tool calling. Bez pytan (automatyzacja): `LLM_PROVIDER=gemini LLM_API_KEY=... bash scripts/install-server.sh -y`.
Nowy serwer w chmurze: [deploy/cloud-init/user-data.yaml](./deploy/cloud-init/user-data.yaml).
Kubernetes: [deploy/kubernetes](./deploy/kubernetes/README.md). Wszystkie tryby: [docs/deploy.md](./docs/deploy.md).

### 2. Podlacz CLI ze swojego laptopa

```powershell
# Windows
.\install.ps1
```

```bash
# Linux / macOS
bash install.sh
```

Od teraz w kazdym terminalu wpisujesz `pipe`. CLI zestawia tunel SSH i laczy sie z agentem.

Wolisz przegladarke? `pipe web` otwiera interfejs z rozmowa, **schematem serwera na zywo** (widac, na ktorym
elemencie agent wlasnie pracuje), podgladem zmian z cofaniem i **przelacznikiem providerow LLM** (dodajesz klucze
w przegladarce i skaczesz miedzy modelami w trakcie rozmowy). Dziala lokalnie, przez ten sam tunel -- na serwerze
nie otwiera sie zaden port.

### 3. Telegram (opcjonalnie, polecane -- tu przychodza alerty)

```bash
cd pipe/clients/telegram
cp .env.example .env    # TELEGRAM_BOT_TOKEN i TELEGRAM_ALLOWED_USER_IDS
sudo bash ../../scripts/install-server.sh   # uruchomi tez bota
```

---

## Co mozesz napisac

- *"pokaz mi architekture serwera"* -- diagram jako obraz; *"narysuj, jak zapytanie trafia do sklepu"* -- wlasny diagram agenta
- *"dlaczego sklep dziala wolno?"* -- diagnoza: logi, zasoby, kontenery
- *"dodaj serwer 10.0.0.5 jako web-2 (ssh, root)"*, potem *"sprawdz dyski i aktualizacje na wszystkich serwerach"* -- workery rownolegle
- *"codziennie o 7 sprawdzaj backupy i waznosc certyfikatow, pisz tylko jak cos jest nie tak"* -- rutyna
- *"przypomnij mi jutro o 9 o odnowieniu domeny"*, *"za godzine sprawdz, czy backup sie skonczyl"* -- przypomnienie przychodzi samo
- *"gdzie lezy repozytorium bloga?"* -- odpowiedz z DIRECTORY
- *"odpowiadaj krocej i bez wstepow"* -- zapisze to w VIBE
- *"strona padla po nocy -- co sie zmienilo?"* -- agent zaczyna od historii zmian: pakiety, obrazy, porty, konfiguracje
- *"czy RAM rosnie od tygodnia?"* -- wykres z historii czuwania
- *"jak bezpieczny jest ten serwer?"* albo `/audyt`, potem *"napraw 1"* -- poprawka z kopia i weryfikacja
- *"postaw strone shop.example.com na porcie 3000 z certyfikatem"* -- wbudowany skill `nginx-vhost`
- wiadomosc glosowa na Telegramie: *"sprawdz, czemu sklep nie dziala"* -- transkrypcja i diagnoza
- *"cofnij ostatnia zmiane"* albo `/cofnij` -- przywraca pliki i odwraca operacje z dziennika

Komendy w obu klientach: `/status` `/raport` `/zmiany` `/wykres` `/zdrowie` `/mapa` `/server` `/katalogi` `/skille`
`/audyt` `/incydenty` `/zgody` `/mcp` `/alerty` `/rutyny` `/przypomnienia` `/cele` `/vibe` `/dziennik` `/cofnij` `/koszt` `/historia` `/pomoc`.

---

## Jezyk

Pipe domyslnie mowi po polsku. Jeden przelacznik zmienia wszystko na angielski: prompty i opisy narzedzi agenta,
komunikaty potwierdzen, alerty, poranny raport, audyt, wbudowane skille, kreator i obu klientow.

| Gdzie | Jak |
|-------|-----|
| Backend | `PIPE_LANG=en` w `backend/.env` (kreator pyta o jezyk; `scripts/install-server.sh --lang en`) |
| Bot Telegrama | `PIPE_LANG=en` w `clients/telegram/.env` (instalator przepisuje z backendu) |
| CLI | `pipe --lang en` albo `export PIPE_LANG=en` |

Komendy maja angielskie nazwy (`/report`, `/changes`, `/undo`...), a polskie dzialaja w obu jezykach.
Znaczniki protokolu (`[POTWIERDZ]`, `[BLAD]`, `[ZREDAGOWANO: ...]`) nie zaleza od jezyka -- klienci zamieniaja je
na etykiety. Zmiana jezyka na dzialajacym serwerze: ustaw `PIPE_LANG` i zrestartuj backend oraz bota; wbudowane
skille w nowym jezyku dojda obok dotychczasowych. Opis po angielsku: [README.en.md](./README.en.md).

---

## Architektura

| Komponent | Gdzie dziala | Polaczenie z backendem |
|-----------|--------------|------------------------|
| `backend/` | Serwer (Docker / systemd / Kubernetes) | -- to jest backend |
| `clients/cli/` | Twoj laptop | tunel SSH -> TCP `127.0.0.1:7379` (albo `kubectl port-forward`) |
| `clients/telegram/` | Serwer | Unix socket |
| `clients/webui/` | Twoj laptop (`pipe web`) | strona na `127.0.0.1:7400`, ten sam tunel SSH co CLI |
| `clients/discord/` | -- | Placeholder -- PR welcome |

```
CLI / Telegram ──JSON lines──> server.py ──> agent (petla LLM + narzedzia)
                                  │              ├─ handlers ─> security (safe/confirm/forbidden) ─> executor
                                  │              ├─ workery ─> cele: ssh / docker exec / kubectl
                                  │              └─ diagram ─> infra (odkrycie) ─> Mermaid ─> PNG
                                  └── czuwanie + rutyny ──(subscribe)──> alerty na Telegram
```

Protokol: JSON lines, ramki z tekstem, zalacznikami (diagramy PNG), postepem workerow i zdarzeniami
czuwania -- [docs/protocol.md](./docs/protocol.md).

---

## Narzedzia agenta

| Narzedzie | Opis |
|-----------|------|
| `execute_command` | Komenda shell na serwerze (przez klasyfikator) |
| `read_file` / `write_file` | Odczyt (sekrety redagowane) i zapis plikow (zawsze z potwierdzeniem) |
| `change_directory` | Katalog roboczy |
| `git_command` | Git: status, log, diff (od razu); pull, commit, push (z potwierdzeniem) |
| `system_stats` | CPU, RAM, dyski, procesy -- z `/proc` hosta |
| `docker_manage` | Kontenery, obrazy, projekty compose |
| `network_info` | Porty hosta (z oznaczeniem publicznych), polaczenia, ping, curl, DNS |
| `cron_manage` | Cron hosta |
| `diagram` | Mapa infrastruktury albo wlasny diagram Mermaid -> obraz dla uzytkownika |
| `target_manage` / `remote_exec` | Zdalne cele (SSH, kontenery, Kubernetes) i komendy na nich |
| `delegate` | Workery: rownolegli pod-agenci, tylko odczyty, raport dla agenta |
| `routine_manage` | Zadania wedlug harmonogramu z raportem na Telegram |
| `reminder` | Jednorazowe przypomnienie albo zadanie o okreslonym czasie ("napisz za 10 minut") |
| `pipe_update` | Aktualizacja samego Pipe: sprawdzenie wersji i przebudowa w osobnym kontenerze ("zaktualizuj sie") |
| `mcp_manage` | Zewnetrzne serwery MCP i ich narzedzia (`mcp__<serwer>__<narzedzie>`, domyslnie z potwierdzeniem) |
| `security_audit` | Audyt bezpieczenstwa z ocena i gotowymi poprawkami |
| `journal` | Dziennik zatwierdzonych zmian z kopiami i cofanie (`/cofnij`) |
| `server_history` | Co sie zmienilo na serwerze (i kiedy), wykresy load/RAM/dyskow, certyfikaty/strony/DNS/backupy |
| `server_md` / `directory` / `skill_manage` / `vibe` | Pamiec agenta |

---

## Pamiec agenta

Wszystko lezy w `backend/data/` na serwerze (poza gitem, mozna edytowac recznie):

- **SERVER.md** -- fakty o serwerze: uslugi, domeny, decyzje. W prompcie kazdej rozmowy.
- **DIRECTORY** (`directory.json`) -- mapa miejsc: repozytoria (z remote i galezia), katalogi aplikacji, projekty compose, konfiguracje, dane, logi, backupy. Skan sam znajduje repozytoria i projekty compose.
- **Skille** (`skills/<nazwa>/SKILL.md`) -- procedury; kazdy skill ma wlasna komende `/nazwa`. Na start Pipe ma
  wbudowane: `nginx-vhost`, `swap`, `fail2ban-ssh`, `backup-postgres`, `aktualizuj-kontener`, `utwardz-ssh`,
  `wolne-miejsce` -- mozesz je zmieniac i usuwac.
- **VIBE** (`vibe/<uzytkownik>.md`) -- jak z Toba rozmawiac. Aktualizuje sie w tle co kilka wiadomosci; `/vibe` pokazuje, `/vibe reset` czysci.
- **Cele, rutyny, przypomnienia** (`targets.json`, `routines.json`, `reminders.json`) i audit log.

---

## Bezpieczenstwo

- **Klasyfikator fail-closed** -- `safe` tylko dla rozpoznanych odczytow (tokenowo: `ss` to nie `ssh`), przekierowania, `$(...)`, flagi typu `find -delete` czy `curl -o` -> pytanie o zgode.
- **Potwierdzenie pokazuje dokladnie to, co sie wykona** -- takze dla komend na zdalnych celach.
- **Redakcja sekretow** -- klucze API, tokeny, hasla i klucze prywatne w wynikach narzedzi sa zastepowane `[ZREDAGOWANO]` przed wyslaniem do LLM; agent nie moze nadpisac pliku z zredagowana trescia.
- **Workery i rutyny tylko czytaja** -- zmiany wracaja jako propozycje do zatwierdzenia.
- **Audit log** kazdej operacji (bez tresci plikow). `AGENT_TOKEN` chroni socket i port.

Szczegoly i ograniczenia (np. `docker.sock` = uprawnienia roota): [docs/security.md](./docs/security.md).

---

## Dokumentacja

- [Szybki start](./docs/quickstart.md)
- [Wdrozenie: Docker, native, Kubernetes, chmury](./docs/deploy.md)
- [Backend](./docs/backend.md)
- [Workery, cele, rutyny, czuwanie, diagramy](./docs/features.md)
- [CLI](./docs/cli.md)
- [Telegram](./docs/telegram.md)
- [Bezpieczenstwo](./docs/security.md)
- [Protokol komunikacji](./docs/protocol.md)
- [Historia zmian](./docs/changelog.md)
