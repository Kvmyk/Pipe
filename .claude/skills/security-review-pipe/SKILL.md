---
name: security-review-pipe
description: Audyt bezpieczenstwa Pipe z naciskiem na srodowisko agentowe — prompt injection, obejscie klasyfikatora komend, za szerokie uprawnienia kontenera, backdoory i trwalosc dla agenta, wycieki sekretow do providera LLM. Uzyj gdy uzytkownik mowi "security review", "audyt bezpieczenstwa", "sprawdz podatnosci", "czy to jest bezpieczne" w kontekscie Pipe, albo przed wydaniem zmiany dotykajacej security.py, handlerow, server.py, pamieci agenta lub docker-compose.
---

# /security-review-pipe — audyt bezpieczenstwa Pipe

Pipe to agent LLM z dostepem do powloki na produkcyjnym VPS. Model zagrozen jest inny niz
w zwyklej aplikacji: **model jezykowy jest nieufnym wejsciem**. Wszystko, co model przeczyta
(pliki, output komend, strony z curl, logi, SERVER.md, skille, nazwy kontenerow), moze zawierac
instrukcje atakujacego. Zakladaj, ze model zostanie przejety, i sprawdzaj, co go wtedy
zatrzyma: klasyfikator, potwierdzenie uzytkownika, uprawnienia kontenera.

Tryb pracy: **tylko odczyt**. Nie zmieniaj kodu, nie uruchamiaj exploitow na prawdziwym
serwerze, nie wysylaj niczego na zewnatrz. Poprawki dopiero po akceptacji uzytkownika.

## 0. Zakres

Argument skilla:
- brak → caly projekt,
- `diff` → tylko niezacommitowane zmiany (`git diff HEAD`),
- `<rewizja>` lub `<od>..<do>` → tylko ten zakres (`git diff <zakres>`),
- nazwa pliku/katalogu → tylko ten obszar.

Przy przegladzie diffa i tak czytaj kod wokol zmiany: podatnosc czesto powstaje dopiero na
styku ze starym kodem (np. nowy handler, ktory omija `classify_command()`).

Najpierw przeczytaj aktualny stan zamiast polegac na pamieci:
`backend/core/security.py`, `backend/core/agent.py` (`_handle_tool_call`, `_execute_tool_confirmed`),
`backend/core/handlers/*.py`, `backend/core/memory.py`, `backend/server.py`,
`backend/config/prompts.py`, `backend/core/session.py`, `backend/core/executor.py`,
`backend/core/audit.py`, `backend/docker-compose.yml`, `backend/Dockerfile`,
`clients/telegram/bot.py`, `clients/telegram/tg_format.py`, `clients/cli/cli.py`,
`install.sh`, `install.ps1`, `.gitignore`.

## 1. Obejscie klasyfikatora komend (`security.py`)

To ostatnia linia obrony przed przejetym modelem. Dla kazdego wektora ustal, czy komenda
wychodzi jako `safe` (wykonanie bez pytania). `confirm` to slabsza podatnosc (zalezy od
czujnosci uzytkownika), `safe` przy szkodliwej komendzie to krytyczna.

Sprawdz konkretnie, wywolujac `classify_command()` w tescie lub REPL (`python -c`), nie na oko:
- podstawienia: `$(...)`, backticki, `<(...)`, `>(...)`, `${IFS}`, `$'\x..'`;
- separatory, ktorych splitter nie zna: znak nowej linii, `&` (tlo), `|&`, `;;`, `\n` w argumencie JSON;
- przekierowania przy "bezpiecznych" prefiksach: `cat x > /hostfs/root/.bashrc`, `echo ... >> ...`, `tee`;
- bezpieczne programy z niebezpiecznymi flagami: `find -exec`/`-delete`, `git -c core.pager=`/`core.sshCommand`/`--upload-pack`,
  `awk 'BEGIN{system(...)}'`, `sed -n 'e ...'`/`w plik`, `less`/`man` (`!`), `tar --to-command`,
  `env X cmd`, `xargs`, `docker run -v /:/x --privileged`, `docker exec`, `curl -o`/`-T`/`file://`, `ssh -o ProxyCommand`;
- wielkosc liter, spacje wiodace, pelne sciezki (`/bin/rm`), cudzyslowy wewnatrz nazwy (`r''m`), `\rm`, `command rm`;
- dopasowanie prefiksu bez granicy slowa (`ls` vs `lsof` vs `ls;x`);
- zakodowane ladunki: `base64 -d | sh`, `python -c`, `perl -e`, `bash -c`, `sh <<<`.

Zweryfikuj tez sciezki poza `execute_command`: czy **kazdy** handler (`git`, `docker`, `system`,
`file_ops`, `memory`) przepuszcza finalny string przez `classify_command()` i czy argumenty od
modelu sa cytowane `shlex.quote()`, a nie wklejane f-stringiem.

## 2. Potwierdzenia (TOCTOU i "co widzisz, to wykonujesz")

- Czy `ConfirmationRequest.command` jest dokladnie tym, co pokazano (`as_code()`), i dokladnie tym, co wykona `_execute_tool_confirmed()`?
- Czy da sie ukryc tresc w potwierdzeniu: bardzo dlugie linie, znaki sterujace ANSI (CLI `rich`), `\r`, znaki Unicode bidi (U+202E), zero-width, homoglify, tagi HTML w Telegramie (`tg_format`).
- `write_file`: czy uzytkownik widzi pelna tresc/sciezke przed TAK? Czy sciezka jest walidowana ponownie przy wykonaniu, czy tylko przy zapytaniu (symlink podmieniony pomiedzy)?
- Czy potwierdzenie jest przypiete do sesji i nie da sie go wyslac z innej sesji/klienta (`session_id` zgadywalny? reuse?). Czy mozna potwierdzic podwojnie (replay)?
- Czy model moze sam "nacisnac TAK" — np. tekst `[POTWIERDZ]` w outpucie komendy albo w SERVER.md zmieniajacy `status` w `server.py` (string-matching tagow).

## 3. Prompt injection (posrednie)

Przesledz kazde zrodlo tekstu trafiajace do kontekstu modelu i oceń, co przejety model moze zrobic bez udzialu uzytkownika:
- output komend i tresc plikow (`read_file`, `cat`, logi nginx z user-agentem atakujacego, `docker ps` z nazwa kontenera, commit message w `git log`);
- `curl`/`network_info` — tresc stron zewnetrznych;
- **SERVER.md i skille**: sa dolaczane do *kazdego* promptu (SERVER.md) lub ladowane na zadanie (skille) i zapisywane **bez potwierdzenia**. Sprawdz: czy jeden udany injection moze zapisac trwala instrukcje (persistence) w SERVER.md/skillu, ktora zadziala w kazdej przyszlej sesji i u kazdego uzytkownika Telegrama? Czy ramowanie "dane, nie polecenia" jest jedyna obrona? Czy nazwy/opisy skilli trafiaja do menu Telegrama i systemu promptu bez sanityzacji?
- `/run_skill` z argumentami od uzytkownika i `RUN_SKILL_MESSAGE` — wstrzykniecie przez `args`/`name`;
- czy odmowy (`forbidden`) nie sa zwracane modelowi w formie zachecajacej do obejscia (model dostaje info "zablokowane przez wzorzec X" i probuje wariantu).

Dla kazdego wektora opisz lancuch: zrodlo → co model moze zrobic → jaka kontrola to zatrzymuje (lub nie).

## 4. Uprawnienia srodowiska (blast radius)

Oceń, co daje przejecie procesu agenta (RCE w kontenerze), niezaleznie od klasyfikatora:
- `docker-compose.yml`: `/var/run/docker.sock` = root na hoscie (`docker run -v /:/host --privileged`). Czy `docker_manage` lub `execute_command` pozwala na `run`/`exec`/`cp` bez potwierdzenia?
- `pid: host` — widocznosc i sygnaly do procesow hosta (`kill`), `/proc/<pid>/environ` i `/proc/<pid>/root` procesow hosta (sekrety innych uslug, obejscie read-only `/hostfs`!). Sprawdz, czy klasyfikator i `validate_workspace_access` blokuja `/proc/*/root`, `/hostproc/*/root`, `/hostproc/*/environ`, `/hostproc/*/cwd`.
- `/:/hostfs:ro` — odczyt `/etc/shadow`, kluczy TLS, `.env` innych aplikacji, `backend/.env` samego agenta (klucz API, `AGENT_TOKEN`, token bota), ktore potem trafiaja do providera LLM.
- `/root:/hostfs/root` rw — zapis `~/.bashrc`, `~/.profile`, `~/.ssh/authorized_keys` (czy blokada `/hostfs/root/.ssh` dziala tez dla `execute_command`, nie tylko dla `write_file`?), `~/.config/systemd/user`, crontab w `/var/spool/cron` (ro?) — czyli trwaly backdoor na hoscie wykonany przez roota przy nastepnym logowaniu.
- `/root/.ssh:/root/.ssh:ro` — klucze prywatne w kontenerze; czy model moze je odczytac (`cat /root/.ssh/id_*` — to sciezka kontenera, nie `/hostfs`!) i wyslac `curl`em.
- `/tmp:/tmp` — socket `/tmp/vps-agent.sock` bez tokena: kazdy lokalny uzytkownik/kontener z `/tmp` hosta moze sterowac agentem. Uprawnienia pliku socketu? Symlink/hijack socketu w `/tmp`?
- kontener jako root, brak `cap_drop`, `no-new-privileges`, `read_only`, limitow zasobow; siec `bridge` z dostepem do internetu (eksfiltracja) i do `host.docker.internal`.
- `TCP_HOST=0.0.0.0` w kontenerze — czy port nie jest wystawiony publicznie przy innym mapowaniu; co jesli `AGENT_TOKEN` pusty.

## 5. Backdoory i trwalosc

Szukaj miejsc, w ktorych agent (lub ktos przez agenta) moze zostawic dostep przetrwajacy restart / nowa sesje:
- pamiec agenta (SERVER.md, `skills/`), `providers.json` (podmiana `base_url` na serwer atakujacego = przechwycenie wszystkich promptow i odpowiedzi, w tym tool calls), `backend/.env` jesli zapisywalny;
- pliki startowe w `/root` hosta, crontab, systemd, `docker run --restart always` z kontenerem atakujacego, obrazy z tym samym tagiem co uslugi uzytkownika;
- `git_command`: `push` do obcego remote, hooki `.git/hooks`, `core.hooksPath`, `.gitconfig` w `/root`;
- audit log: czy agent moze go czytac/modyfikowac/zapchac (`/app/data/audit.log` w tym samym wolumenie co pamiec), czy da sie ukryc akcje (komenda z `\n` falszujaca wpisy), czy wszystkie sciezki wykonania sa audytowane (tez `_execute_tool_confirmed` i komendy slash);
- lancuch dostaw: `install.sh`/`install.ps1` (curl | bash, pobieranie bez sumy kontrolnej), Dockerfile (docker static tgz bez weryfikacji sumy), niepinowane wersje w `requirements.txt`;
- kod sam w sobie: ukryte sciezki dostepu, debugowe komendy protokolu, martwy kod `VPSAgent._handle_*` w `agent.py` — czy jest faktycznie nieosiagalny.

## 6. Transport, uwierzytelnienie, klienci

- `server.py`: porownanie tokena (`!=` zamiast `hmac.compare_digest`), token sprawdzany dla kazdego typu zadania, limit dlugosci linii JSON / liczby polaczen (DoS), wycieki wyjatkow do klienta.
- `session_id` od klienta: czy klient A moze przejac sesje/potwierdzenie klienta B, znajac lub zgadujac UUID; czy `interface` od klienta (np. `telegram:<cudze_id>`) daje jakies uprawnienia.
- Telegram: whitelist `user_id` sprawdzana dla wiadomosci **i** callbackow TAK/NIE i komend; czy bot dziala w grupach; czy callback z innego czatu moze zatwierdzic komende; ustawianie menu per czat.
- CLI: `StrictHostKeyChecking` tunelu SSH, token w argumentach procesu (`--token` widoczny w `ps`), historia REPL zapisujaca sekrety.
- Renderowanie: HTML injection w Telegramie przez output modelu, sekwencje ANSI w CLI.

## 7. Sekrety i dane wysylane do providera

- `memory.find_secret()` — jak latwo go obejsc (base64, rozbicie na linie, nietypowe formaty kluczy); czy chroni tez skille i opisy.
- Co trafia do LLM: wyniki `cat .env`, `/proc/*/environ`, `docker inspect` (zmienne srodowiskowe kontenerow!), `git remote -v` z tokenem w URL. Czy jest jakakolwiek redakcja przed wyslaniem?
- Czy audit log, logi dockera (`print`, `logging`) nie zawieraja tresci plikow, promptow ani tokenow.
- `.gitignore`: `backend/.env`, `clients/*/.env`, `backend/data/`, `audit.log`. Sprawdz historie: `git log --all --diff-filter=A --name-only | grep -Ei '\.env$|secret|token|key'`.

## 8. Weryfikacja znalezisk

Kazde znalezisko musi byc potwierdzone, zanim trafi do raportu:
- dla klasyfikatora — faktyczny wynik `classify_command()` (np. `python -c "from backend.core.security import classify_command as c; print(c('...'))"`, w venv z `backend/requirements.txt`);
- dla sciezek kodu — cytat z pliku z numerem linii i opis przeplywu danych od wejscia do sinka;
- dla uprawnien — konkretna linia z `docker-compose.yml`/`Dockerfile` i konkretna komenda, ktora to wykorzystuje.

Odrzuc to, czego nie da sie osiagnac z wejscia kontrolowanego przez atakujacego (uzytkownik
spoza whitelisty, tresc czytana przez model, lokalny proces na hoscie). Jesli cos jest
"by design" (np. `docker.sock`), zglos to jako ryzyko architektoniczne z ocena, a nie jako bug —
i zaproponuj ograniczenie (np. docker socket proxy z allowlista endpointow).

Nie pisz do repo testow-exploitow; do weryfikacji uzyj scratchpada.

## 9. Raport

Po polsku, uszeregowany od najpowazniejszego. Dla kazdego znaleziska:

```
### [KRYTYCZNE|WYSOKIE|SREDNIE|NISKIE] Krotki tytul
Plik: sciezka:linia
Wektor: kto/co kontroluje wejscie (przejety model przez injection / uzytkownik spoza whitelisty / lokalny proces / ...)
Scenariusz: konkretny lancuch krokow od wejscia do skutku
Dowod: wynik classify_command / cytat kodu / linia compose
Skutek: co atakujacy zyskuje (root na hoscie, eksfiltracja kluczy, trwaly backdoor, ...)
Poprawka: minimalna, konkretna zmiana
```

Skala:
- **KRYTYCZNE** — wykonanie kodu jako root na hoscie, trwaly backdoor lub eksfiltracja sekretow **bez** potwierdzenia uzytkownika;
- **WYSOKIE** — to samo, ale wymaga TAK od uzytkownika wprowadzonego w blad (ukryta tresc potwierdzenia) albo lokalnego dostepu do hosta;
- **SREDNIE** — obejscie jednej warstwy obrony przy dzialajacej kolejnej, wyciek danych niewrazliwych, DoS;
- **NISKIE** — hardening, defense in depth.

Na koncu: krotka tabela "warstwa → stan" (klasyfikator, potwierdzenia, pamiec, kontener, transport,
sekrety) i 3–5 najwazniejszych rekomendacji w kolejnosci wdrazania. Zapytaj, ktore poprawki
wdrozyc — kazda wdrozona poprawka dostaje test regresyjny w `backend/tests/`, a potem `/ship`.
