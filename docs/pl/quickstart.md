# Szybki start -- instrukcja krok po kroku

Pipe v0.49.2

## Wypróbuj bez serwera

Nie masz wolnego serwera albo chcesz najpierw zobaczyć, jak Pipe działa? Na swoim komputerze z Dockerem:

```bash
git clone https://github.com/Kvmyk/pipe && cd pipe
LLM_PROVIDER=gemini LLM_API_KEY=... python3 sandbox/run.py try --scenario nginx-502
pipe --no-tunnel --local-port 7390        # albo: pipe web --no-tunnel --local-port 7390
```

Startuje kontener udający mały serwer ze sklepem (nginx i aplikacja), a w nim Pipe. Z `--scenario` serwer jest od
razu zepsuty: `nginx-502` (zły port w nginx), `app-down` (aplikacja nie startuje), `static-403` (źle uprawnienia),
`disk-hog` (zapomniany duzy log). Poproś Pipe o pomoc i zobacz, jak szuka przyczyny, pyta o zgodę i robi kopię.
Bez klucza piaskownica też wstaje, wtedy Pipe działa bez modelu. Kończysz przez `docker rm -f pipe-sandbox`.

Te same scenariusze służą do oceny modeli: `python3 sandbox/run.py eval` uruchamia każdy, zgadza się na każdą
proponowaną zmianę i sprawdza, czy serwer naprawdę został naprawiony (szczegóły: `sandbox/README.md`).

## Wymagania wstępne

1. **Na serwerze** -- uruchomiony backend agenta
2. **Na laptopie** -- Python 3.11+ i dostęp do SSH

Krótsza droga: zainstaluj klienta (krok 2, adres serwera możesz pominąć) i wpisz `pipe web`. Strona zapyta, co chcesz
robić, i sama postawi Pipe na tym komputerze (Docker) albo na Twoim serwerze (adres i hasło albo klucz SSH). Wtedy
krok 1 i 3 dzieją się same.

---

## Krok 1: Uruchom backend na serwerze

Zaloguj się przez SSH na serwer i wykonaj:

```bash
git clone https://github.com/Kvmyk/pipe
cd pipe
sudo bash scripts/install-server.sh --lang pl  # albo z --mode native (bez Dockera)
```

Kreator zapyta o providera LLM i klucz API, pobierze aktualną listę modeli, sprawdzi, czy wybrany model
obsługuje tool calling, i zapisze `backend/.env`. Nie masz klucza? Wybierz **Google Gemini** --
darmowy klucz wygenerujesz na https://aistudio.google.com/apikey (w darmowym tierze Google może uczyć się na
zapytaniach, więc na produkcji lepiej włączyć płatności). Możesz też wybrać **Bez modelu**: Pipe pilnuje serwera,
wysyła alerty i raporty, a nic nie wychodzi z serwera; rozmowę włączysz później
([backend.md](backend.md#bez-modelu-llm_providernone)).

Skrypt zainstaluje Dockera (jeśli go nie ma) i uruchomi backend. Ręcznie to samo:
`python3 -m backend.configure && cd backend && docker compose up -d --build`.

Sprawdź czy działa:
```bash
cd backend && docker compose logs vps-agent
# Powinno pokazac: [VPS Agent] Serwer gotowy.
```

Nowy serwer w chmurze możesz postawić od razu z Pipe -- [cloud-init](../../deploy/cloud-init/user-data.yaml).
Kubernetes: [deploy/kubernetes](../../deploy/kubernetes/README.md).

---

## Krok 2: Zainstaluj CLI na laptopie

```bash
git clone https://github.com/Kvmyk/pipe
cd pipe
```

### Windows (PowerShell)

```powershell
.\install.ps1
# Skrypt zapyta o adres serwera: np. root@serwer.example.com
```

### Linux / macOS (bash/zsh)

```bash
bash install.sh
# Skrypt zapyta o adres serwera: np. root@serwer.example.com
```

Skrypt:
- zainstaluje zależności Python (`rich`)
- doda funkcję `pipe` do Twojego profilu terminala
- od teraz wystarczy wpisać `pipe` w dowolnym terminalu

---

## Krok 3: Połącz się z agentem

```
pipe
```

CLI automatycznie:
1. Zestawia tunel SSH (zapyta o hasło jeśli potrzeba)
2. Łączy się z agentem na serwerze
3. Czeka na Twoje polecenia

Na początek: `/mapa` (diagram tego, co stoi na serwerze) i `/server` (agent zbada serwer i zapisze,
co wie, w SERVER.md i DIRECTORY).

---

## Przykładowa sesja

```
 pipe   Autonomiczny agent AI do zarzadzania serwerem Linux

 serwer    root@serwer.example.com
 komendy   /status  /raport  /zmiany  /mapa  /server  /skille  /pomoc  /exit

Polaczono z agentem na root@serwer.example.com
──────────────────────────────────────────────────────────────────────

› ile mam wolnego miejsca na dysku?

Dysk jest zapelniony w 79% -- zostalo Ci okolo 4.2 GB.
Najwieksze katalogi to /var/log (1.1 GB) i /home (2.3 GB).
Czy chcesz wyczyscic logi? (`journalctl --vacuum-time=7d`)

› pokaz bledy nginx z ostatniej godziny

› zrestartuj nginx

╭─  Potrzebuję Twojej zgody  ──────────────────────────────────────────╮
│                                                                      │
│    $ systemctl restart nginx                                         │
│                                                                      │
│  Bezpiecznik                                                         │
│   ✓  sprawdzenie przed              nginx -t                         │
│      (niepowodzenie = nie wykonam)                                   │
│   ✓  weryfikacja po                 usluga nginx aktywna             │
│                                     strony, ktore dzialaja teraz,    │
│                                     maja dzialac po zmianie          │
╰──────────────────────────────────────────────────────────────────────╯

Wykonać? [t/n] (n): tak
✓ Zatwierdzone — wykonuję...
nginx zrestartowany, weryfikacja przeszla.

› exit
Do widzenia!
```

Komenda w panelu jest dokładnie tym, co zostanie wykonane. Pod nią plan bezpiecznika: co Pipe sprawdzi przed
zmiana i po niej (szczegóły: [Zmiany z bezpiecznikiem](features.md#zmiany-z-bezpiecznikiem-i-cofnij)).

---

## Opcje skrótu `pipe`

Możesz też przekazać dodatkowe opcje:

```bash
pipe --ssh-port 2222      # niestandardowy port SSH
pipe --key ~/.ssh/id_rsa  # konkretny klucz SSH
pipe --session moja-sesja # zachowaj historię sesji
pipe --lang pl            # komunikaty CLI po polsku (bez tego CLI bierze jezyk serwera)
```

---

## Problemy

| Problem | Rozwiązanie |
|---------|-------------|
| Agent nie odpowiada / błąd modelu | `python3 -m backend.configure --check` -- sprawdzi klucz, model i tool calling |
| W logach "Model ... nie wystepuje na liście" | Model został wycofany -- wybierz nowy: `python3 -m backend.configure` |
| Tunel SSH nie odpowiada | Sprawdź czy backend działa: `docker logs vps-agent` na serwerze |
| Brak komendy 'ssh' | Windows: zainstaluj OpenSSH Client (Ustawienia -> Aplikacje -> Funkcje opcjonalne) |
| SSH pyta o hasło przy każdym uruchomieniu | Dodaj klucz SSH: `ssh-copy-id user@serwer` |
