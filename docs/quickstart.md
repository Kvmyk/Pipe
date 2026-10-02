# Szybki start -- instrukcja krok po kroku

Pipe v0.20.1

## Wymagania wstepne

1. **Na serwerze** -- uruchomiony backend agenta
2. **Na laptopie** -- Python 3.11+ i dostep do SSH

---

## Krok 1: Uruchom backend na serwerze

Zaloguj sie przez SSH na serwer i wykonaj:

```bash
git clone https://github.com/user/pipe
cd pipe
sudo bash scripts/install-server.sh            # albo --mode native (bez Dockera)
```

Kreator zapyta o providera LLM i klucz API, pobierze aktualna liste modeli, sprawdzi, czy wybrany model
obsluguje tool calling, i zapisze `backend/.env`. Nie masz klucza? Wybierz **Google Gemini** --
darmowy klucz wygenerujesz na https://aistudio.google.com/apikey

Skrypt zainstaluje Dockera (jesli go nie ma) i uruchomi backend. Recznie to samo:
`python3 -m backend.configure && cd backend && docker compose up -d --build`.

Sprawdz czy dziala:
```bash
cd backend && docker compose logs vps-agent
# Powinno pokazac: [VPS Agent] Serwer gotowy.
```

Nowy serwer w chmurze mozesz postawic od razu z Pipe -- [cloud-init](../deploy/cloud-init/user-data.yaml).
Kubernetes: [deploy/kubernetes](../deploy/kubernetes/README.md).

---

## Krok 2: Zainstaluj CLI na laptopie

```bash
git clone https://github.com/user/pipe
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
- zainstaluje zaleznosci Python (`rich`)
- doda funkcje `pipe` do Twojego profilu terminala
- od teraz wystarczy wpisac `pipe` w dowolnym terminalu

---

## Krok 3: Polacz sie z agentem

```
pipe
```

CLI automatycznie:
1. Zestawia tunel SSH (zapyta o haslo jesli potrzeba)
2. Laczy sie z agentem na serwerze
3. Czeka na Twoje polecenia

Na poczatek: `/mapa` (diagram tego, co stoi na serwerze) i `/server` (agent zbada serwer i zapisze,
co wie, w SERVER.md i DIRECTORY).

---

## Przykladowa sesja

```
+-------------------------------------------+
|  PIPE                                     |
|  Autonomiczny agent AI                    |
|  Polaczono z: root@serwer.example.com     |
+-------------------------------------------+

-------------- Status serwera ---------------

Serwer serwer17, dzialajacy 3 dni 7 godzin.
Dysk: 4.2 GB wolne z 20 GB (79% zajete)
RAM:  1.1 GB wolne z 2 GB

---------------------------------------------

> ile mam wolnego miejsca na dysku?

Dysk jest zapelniony w 79% -- zostalo Ci okolo 4.2 GB.
Najwieksze katalogi to /var/log (1.1 GB) i /home (2.3 GB).
Czy chcesz wyczyscic logi? (`journalctl --vacuum-time=7d`)

> pokaz bledy nginx z ostatniej godziny

> zrestartuj nginx
WYMAGA POTWIERDZENIA: `systemctl restart nginx`
Czy chcesz wykonac te operacje? [t/N]: t
nginx zrestartowany pomyslnie.

> exit
Do widzenia!
```

---

## Opcje skrotu `pipe`

Mozesz tez przekazac dodatkowe opcje:

```bash
pipe --ssh-port 2222      # niestandardowy port SSH
pipe --key ~/.ssh/id_rsa  # konkretny klucz SSH
pipe --session moja-sesja # zachowaj historie sesji
```

---

## Problemy

| Problem | Rozwiazanie |
|---------|-------------|
| Agent nie odpowiada / blad modelu | `python3 -m backend.configure --check` -- sprawdzi klucz, model i tool calling |
| W logach "Model ... nie wystepuje na liscie" | Model zostal wycofany -- wybierz nowy: `python3 -m backend.configure` |
| Tunel SSH nie odpowiada | Sprawdz czy backend dziala: `docker logs vps-agent` na serwerze |
| Brak komendy 'ssh' | Windows: zainstaluj OpenSSH Client (Ustawienia -> Aplikacje -> Funkcje opcjonalne) |
| SSH pyta o haslo przy kazdym uruchomieniu | Dodaj klucz SSH: `ssh-copy-id user@serwer` |
