# Szybki start -- instrukcja krok po kroku

PipeClaw v0.4.1

## Wymagania wstepne

1. **Na serwerze (Mikrus)** -- uruchomiony backend agenta
2. **Na laptopie** -- Python 3.11+ i dostep do SSH

---

## Krok 1: Uruchom backend na serwerze

SSH na Mikrusa i wykonaj:

```bash
git clone https://github.com/user/pipeclaw
cd pipeclaw/backend

cp .env.example .env
nano .env   # wpisz swoj LLM_API_KEY (darmowy: https://aistudio.google.com/)

docker-compose up -d
```

Sprawdz czy dziala:
```bash
docker logs vps-agent
# Powinno pokazac: [VPS Agent] Serwer gotowy.
```

---

## Krok 2: Zainstaluj CLI na laptopie

```bash
git clone https://github.com/user/pipeclaw
cd pipeclaw
```

### Windows (PowerShell)

```powershell
.\install.ps1
# Skrypt zapyta o adres serwera: np. root@mikrus.example.com
```

### Linux / macOS (bash/zsh)

```bash
bash install.sh
# Skrypt zapyta o adres serwera: np. root@mikrus.example.com
```

Skrypt:
- zainstaluje zaleznosci Python (`rich`)
- doda funkcje `pipeclaw` do Twojego profilu terminala
- od teraz wystarczy wpisac `pipeclaw` w dowolnym terminalu

---

## Krok 3: Polacz sie z agentem

```
pipeclaw
```

CLI automatycznie:
1. Zestawia tunel SSH (zapyta o haslo jesli potrzeba)
2. Laczy sie z agentem na serwerze
3. Wyswietla status serwera (hostname, uptime, dysk, RAM)
4. Czeka na Twoje polecenia

---

## Przykladowa sesja

```
+-------------------------------------------+
|  PIPECLAW                                     |
|  Autonomiczny agent AI                    |
|  Polaczono z: root@mikrus.example.com     |
+-------------------------------------------+

-------------- Status serwera ---------------

Serwer mikrus17, dzialajacy 3 dni 7 godzin.
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

## Opcje skrotu `pipeclaw`

Mozesz tez przekazac dodatkowe opcje:

```bash
pipeclaw --ssh-port 2222      # niestandardowy port SSH
pipeclaw --key ~/.ssh/id_rsa  # konkretny klucz SSH
pipeclaw --session moja-sesja # zachowaj historie sesji
```

---

## Problemy

| Problem | Rozwiazanie |
|---------|-------------|
| Tunel SSH nie odpowiada | Sprawdz czy backend dziala: `docker logs vps-agent` na serwerze |
| Brak komendy 'ssh' | Windows: zainstaluj OpenSSH Client (Ustawienia -> Aplikacje -> Funkcje opcjonalne) |
| SSH pyta o haslo przy kazdym uruchomieniu | Dodaj klucz SSH: `ssh-copy-id user@serwer` |
