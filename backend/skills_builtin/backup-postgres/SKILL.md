---
name: backup-postgres
description: Codzienny backup bazy PostgreSQL (pg_dump) z rotacja 7 dni, wpisem [backup] w DIRECTORY i pilnowaniem swiezosci.
---

# Backup PostgreSQL

## 1. Rozpoznanie (odczyty)
- Gdzie jest baza: kontener (docker_manage ps — obraz postgres) czy usluga hosta (`systemctl status postgresql`).
- Nazwa bazy i uzytkownik: z pliku compose/.env aplikacji (DIRECTORY) — NIE przepisuj hasel do skilla ani SERVER.md.
- Wolne miejsce na docelowy katalog (domyslnie /var/backups/pg) i rozmiar bazy:
  `docker exec KONTENER psql -U USER -d BAZA -Atc "select pg_size_pretty(pg_database_size(current_database()))"`.

## 2. Skrypt /usr/local/bin/pipe-backup-pg (write_file, potem chmod 700)
Dla kontenera:
```
#!/bin/sh
set -eu
DEST=/var/backups/pg
mkdir -p "$DEST"
docker exec KONTENER pg_dump -U USER -Fc BAZA > "$DEST/BAZA-$(date +%F-%H%M).dump"
find "$DEST" -name 'BAZA-*.dump' -mtime +7 -delete
```
Dla uslugi hosta zamiast `docker exec KONTENER` uzyj `sudo -u postgres`.

## 3. Harmonogram
Cron hosta (cron_manage add w trybie native; w trybie docker — podaj wpis do dodania na hoscie):
`30 3 * * * /usr/local/bin/pipe-backup-pg`

## 4. Pilnowanie
- directory upsert path=/var/backups/pg kind=backup description="nocne dumpy PostgreSQL BAZA, rotacja 7 dni"
  — od teraz czuwanie alarmuje, gdy najnowszy plik ma wiecej niz 26 h.
- Uruchom skrypt raz recznie (po TAK) i sprawdz: `ls -lh /var/backups/pg` — plik > 0 B.
- Test odtworzenia (zalecany raz): `pg_restore --list PLIK | head`.

Cofniecie: usun wpis crona, skrypt i wpis z DIRECTORY.
