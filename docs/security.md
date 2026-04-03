# Bezpieczenstwo -- PipeClaw

PipeClaw v0.2

## Model bezpieczenstwa

PipeClaw dziala wewnatrz izolowanego kontenera Docker z ograniczonymi uprawnieniami. Kontener ma dostep do:

- `/var/run/docker.sock` -- zarzadzanie kontenerami na hoscie
- `/hostfs` (read-write) -- pelny dostep i modyfikacja systemu plikow hosta
- `/tmp` -- wspolny katalog z hostem (Unix socket)

## Klasyfikacja komend

Kazda komenda jest klasyfikowana na jeden z trzech poziomow:

### Poziom 1: SAFE -- wykonaj od razu

Komendy diagnostyczne i informacyjne, np.:
- `df`, `free`, `uptime`, `ps`, `top`
- `docker ps`, `docker logs`, `docker stats`
- `git status`, `git log`, `git diff`
- `ls`, `cat`, `head`, `tail`, `grep`, `find`
- `ss`, `netstat`, `ping`, `curl`, `nslookup`

### Poziom 2: CONFIRM -- wymagaja potwierdzenia

Komendy modyfikujace system, np.:
- Edycja plikow w `/etc/`
- `chmod`, `chown`, `ufw`, `iptables`
- `rm`, `mv`, `cp -r`
- `docker stop`, `docker rm`, `docker rmi`
- `git push`, `git commit`, `git checkout`, `git merge`
- `pip install`, `pip3 install`
- `crontab` (dodawanie/usuwanie zadan)
- `kill`, `pkill`
- `reboot`, `shutdown`

### Poziom 3: FORBIDDEN -- odmow bezwzglednie

Komendy ktore nigdy nie beda wykonane, niezaleznie od prosby uzytkownika:
- `rm -rf /` -- usuwanie calego systemu plikow
- `rm --no-preserve-root` -- jak wyzej
- `dd if=` -- nadpisywanie urzadzen blokowych
- `mkfs.` -- formatowanie partycji
- Nadpisywanie `/etc/passwd`, `/etc/shadow`
- Fork bomb: `:(){ :|:& };:`
- `curl ... | bash`, `wget ... | bash` -- zdalne wykonywanie kodu
- `python -c ... exec` -- dynamiczne wykonywanie kodu
- Zapis do `/dev/sd*`, `/boot/`

## Domyslna klasyfikacja

Jesli komenda nie pasuje do zadnej z powyzszych kategorii, domyslnie wymaga potwierdzenia (`confirm`). Zasada: lepiej dopytac niz wykonac cos potencjalnie szkodliwego.

## Audit log

Kazda operacja jest zapisywana do append-only audit logu:

```
[2026-03-28 14:23:11] [CLI] [SAFE] systemctl status nginx -> exit_code=0
[2026-03-28 14:25:03] [TELEGRAM:123456789] [CONFIRMED] systemctl restart nginx -> exit_code=0
[2026-03-28 14:26:44] [TELEGRAM:123456789] [BLOCKED] rm -rf / -> FORBIDDEN
```

Zawartosc plikow (read_file, write_file) nigdy nie trafia do logu -- moze zawierac sekrety.

## Izolacja kontenera

Backend agenta dziala w kontenerze Docker z:
- Read-write montowaniem hosta pod `/hostfs`
- Brakiem dostepu do sieci hosta (network_mode: bridge)
- Ograniczonym dostepem do Docker socket (tylko zarzadzanie kontenerami)

## Zalecenia

1. Uzywaj dedykowanego uzytkownika (`vpsagent`) z ograniczonymi uprawnieniami sudo
2. Regularnie przegladaj audit log
3. Ogranicz whiteliste uzytkownikow Telegram do minimum
4. Nie udostepniaj klucza API LLM publicznie
5. Na produkcji uzyj klucza SSH zamiast hasla
