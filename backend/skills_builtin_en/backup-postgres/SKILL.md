---
name: backup-postgres
description: Daily PostgreSQL backup (pg_dump) with 7-day rotation, a [backup] entry in DIRECTORY and freshness monitoring.
---

# PostgreSQL backup

## 1. Discovery (reads)
- Where the database runs: a container (docker_manage ps — postgres image) or a host service (`systemctl status postgresql`).
- Database name and user: from the app's compose/.env file (DIRECTORY) — do NOT copy passwords into the skill or SERVER.md.
- Free space in the destination directory (default /var/backups/pg) and the database size:
  `docker exec CONTAINER psql -U USER -d DB -Atc "select pg_size_pretty(pg_database_size(current_database()))"`.

## 2. Script /usr/local/bin/pipe-backup-pg (write_file, then chmod 700)
For a container:
```
#!/bin/sh
set -eu
DEST=/var/backups/pg
mkdir -p "$DEST"
docker exec CONTAINER pg_dump -U USER -Fc DB > "$DEST/DB-$(date +%F-%H%M).dump"
find "$DEST" -name 'DB-*.dump' -mtime +7 -delete
```
For a host service use `sudo -u postgres` instead of `docker exec CONTAINER`.

## 3. Schedule
Host cron (cron_manage add in native mode; in docker mode — give the entry to add on the host):
`30 3 * * * /usr/local/bin/pipe-backup-pg`

## 4. Monitoring
- directory upsert path=/var/backups/pg kind=backup description="nightly PostgreSQL dumps of DB, 7-day rotation"
  — from now on the watcher alerts when the newest file is older than 26 h.
- Run the script once by hand (after YES) and check: `ls -lh /var/backups/pg` — file > 0 B.
- Restore test (recommended once): `pg_restore --list FILE | head`.

Undo: remove the cron entry, the script and the DIRECTORY entry.
