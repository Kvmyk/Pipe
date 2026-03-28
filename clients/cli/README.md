# CLI — VPS Management Agent

Interaktywny terminal do zarządzania serwerem VPS przez agenta AI.

**Działa na Twoim laptopie** — łączy się zdalnie z backendem uruchomionym na Mikrusie przez automatyczny **tunel SSH**.

## Jak to działa

```
Twój laptop
    └── cli.py
          ├── automatycznie otwiera tunel SSH →
          │       ssh -L 7379:127.0.0.1:7379 user@serwer
          └── łączy się przez tunel z agentem na serwerze
                        ↓
              serwer (Mikrus) — backend/server.py
                        ↓
              /tmp/vps-agent.sock + 127.0.0.1:7379 (tylko localhost)
```

CLI **automatycznie zestawia tunel SSH** — nie musisz nic robić ręcznie.

## Wymagania

- Python 3.11+
- `ssh` dostępny w terminalu (`which ssh`)
- Konto SSH na serwerze Mikrus
- Działający backend na serwerze (`docker-compose up -d` w katalogu `backend/`)

## Instalacja

```bash
pip install -r requirements.txt
```

## Użycie

```bash
# Podstawowe — podaj adres serwera
python cli.py --host root@mikrus.example.com

# Niestandardowy port SSH
python cli.py --host root@1.2.3.4 --ssh-port 2222

# Klucz SSH (jeśli nie masz domyślnego w ~/.ssh/)
python cli.py --host root@1.2.3.4 --key ~/.ssh/id_mikrus

# Zmienna środowiskowa zamiast flagi (wygodne do codziennego użycia)
export VPS_HOST=root@mikrus.example.com
python cli.py
```

## Jeśli masz już własny tunel SSH

```bash
# Ręcznie zestawiasz tunel w tle
ssh -N -L 7379:127.0.0.1:7379 root@mikrus.example.com &

# CLI z wyłączonym auto-tunnel
python cli.py --no-tunnel --local-port 7379
```

## Zmienne środowiskowe

| Zmienna | Opis | Domyślnie |
|---------|------|-----------|
| `VPS_HOST` | Adres serwera (`user@host`) | — |
| `VPS_SSH_PORT` | Port SSH | `22` |
| `VPS_SSH_KEY` | Ścieżka do klucza prywatnego | domyślny klucz |
| `VPS_LOCAL_PORT` | Lokalny port tunelu | `7379` |
| `VPS_REMOTE_SOCKET` | Socket na serwerze | `/tmp/vps-agent.sock` |

## Przykładowe komendy w CLI

```
> ile mam wolnego miejsca na dysku?
> pokaż ostatnie błędy nginx
> zrestartuj docker compose
> sprawdź czy port 80 jest otwarty
> sprawdź użycie RAM przez procesy
```

Wpisz `exit` lub naciśnij `Ctrl+C` aby wyjść.
