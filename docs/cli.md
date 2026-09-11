# CLI -- Pipe

Pipe v0.8.0

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

## Zmienne srodowiskowe

| Zmienna | Opis | Domyslnie |
|---------|------|-----------|
| `VPS_HOST` | Adres serwera (`user@host`) | -- |
| `AGENT_TOKEN` | Token autoryzacji backendu (rownowazny `--token`); wymagany tylko gdy backend ma ustawione `AGENT_TOKEN` | -- |
| `VPS_SSH_PORT` | Port SSH | `22` |
| `VPS_SSH_KEY` | Sciezka do klucza prywatnego | domyslny klucz |
| `VPS_LOCAL_PORT` | Lokalny port tunelu | `7379` |
| `VPS_REMOTE_SOCKET` | Socket na serwerze | `/tmp/vps-agent.sock` |

## Przykladowe komendy w CLI

```
> ile mam wolnego miejsca na dysku?
> pokaz ostatnie bledy nginx
> zrestartuj docker compose
> sprawdz czy port 80 jest otwarty
> sprawdz uzycie RAM przez procesy
> pokaz status repozytoriow git
> wyswietl otwarte porty
> dodaj zadanie cron: backup co 3 godziny
```

Wpisz `exit` lub nacisnij `Ctrl+C` aby wyjsc.

## Komendy

| Komenda | Dzialanie |
|---|---|
| `/status` | Stan serwera |
| `/server` | Pokazuje SERVER.md; gdy go nie ma -- agent bada serwer i tworzy plik |
| `/server aktualizuj` | Agent bada serwer ponownie i aktualizuje SERVER.md |
| `/skille` | Lista zapisanych skilli z ich komendami |
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
dla wszystkich klientow -- to, co agent zapisze w Telegramie, widac w terminalu i odwrotnie.
