#!/bin/sh
# Raport dowodow jest tylko do odczytu: stara kopia zostaje taka, jaka byla, i nic nie przybywa w katalogu.
[ "$(ls -A /var/backups/shop | wc -l)" -eq 1 ] && [ -n "$(find /var/backups/shop/shop-db.sql.gz -mtime +30)" ]
