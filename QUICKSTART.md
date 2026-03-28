# 🚀 Jak zacząć — instrukcja krok po kroku

## Wymagania wstępne

1. **Na serwerze (Mikrus)** — uruchomiony backend agenta
2. **Na laptopie** — Python 3.11+ i dostęp do SSH

---

## Krok 1: Uruchom backend na serwerze

SSH na Mikrusa i wykonaj:

```bash
git clone https://github.com/user/vps-agent
cd vps-agent/backend

cp .env.example .env
nano .env   # wpisz swój LLM_API_KEY (darmowy: https://aistudio.google.com/)

docker-compose up -d
```

Sprawdź czy działa:
```bash
docker logs vps-agent
# Powinno pokazać: [VPS Agent] Serwer gotowy.
```

---

## Krok 2: Zainstaluj CLI na laptopie

```bash
git clone https://github.com/user/vps-agent
cd vps-agent
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
3. Wyświetla status serwera (hostname, uptime, dysk, RAM)
4. Czeka na Twoje polecenia

---

## Przykładowa sesja

```
╭─────────────────────────────────────────╮
│  VPS Management Agent                   │
│  Autonomiczny agent AI                  │
│  Połączono z: root@mikrus.example.com   │
╰─────────────────────────────────────────╯

──────────── Status serwera ────────────

✅ Serwer mikrus17, działający 3 dni 7 godzin.
   Dysk: 4.2 GB wolne z 20 GB (79% zajęte) ⚠️
   RAM:  1.1 GB wolne z 2 GB

────────────────────────────────────────

> ile mam wolnego miejsca na dysku?

Dysk jest zapełniony w 79% — zostało Ci około 4.2 GB.
Największe katalogi to /var/log (1.1 GB) i /home (2.3 GB).
Czy chcesz wyczyścić logi? (`journalctl --vacuum-time=7d`)

> pokaż błędy nginx z ostatniej godziny

> zrestartuj nginx
⚠️ Operacja wymaga potwierdzenia: `systemctl restart nginx`
Czy chcesz wykonać tę operację? [t/N]: t
✅ nginx zrestartowany pomyślnie.

> exit
Do widzenia!
```

---

## Opcje skrótu `pipe`

Możesz też przekazać dodatkowe opcje:

```bash
pipe --ssh-port 2222      # niestandardowy port SSH
pipe --key ~/.ssh/id_rsa  # konkretny klucz SSH
pipe --session moja-sesja # zachowaj historię sesji
```

---

## Problemy

| Problem | Rozwiązanie |
|---------|-------------|
| `❌ Tunel SSH nie odpowiada` | Sprawdź czy backend działa: `docker logs vps-agent` na serwerze |
| `❌ Brak komendy 'ssh'` | Windows: zainstaluj OpenSSH Client (Ustawienia → Aplikacje → Funkcje opcjonalne) |
| SSH pyta o hasło przy każdym uruchomieniu | Dodaj klucz SSH: `ssh-copy-id user@serwer` |
