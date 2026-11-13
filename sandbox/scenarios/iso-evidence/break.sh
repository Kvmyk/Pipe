#!/bin/sh
# Kopia bazy sklepu sprzed 40 dni i nic nowszego: dowod dla ISO A.8.13 to "czesciowo" albo "luka".
mkdir -p /var/backups/shop
echo 'zrzut bazy sklepu' | gzip > /var/backups/shop/shop-db.sql.gz
touch -d "40 days ago" /var/backups/shop/shop-db.sql.gz
