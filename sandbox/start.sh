#!/bin/sh
# Start piaskownicy: sklep w stanie dzialajacym, opcjonalnie zepsuty scenariuszem ($SCENARIO), potem Pipe.
set -e
mkdir -p /var/lib/pipe /var/log/shop /var/www/shop/static /etc/shop
[ -f /var/lib/pipe/SERVER.md ] || cp /opt/sandbox/SERVER.md /var/lib/pipe/SERVER.md
cp /opt/sandbox/app/app.conf /etc/shop/app.conf
cp /opt/sandbox/nginx-shop.conf /etc/nginx/sites-enabled/shop
printf 'logo sklepu\n' > /var/www/shop/static/logo.txt
chmod 644 /var/www/shop/static/logo.txt

if [ -n "${SCENARIO:-}" ]; then
  if [ ! -x "/opt/sandbox/scenarios/$SCENARIO/break.sh" ]; then
    echo "[sandbox] nieznany scenariusz: $SCENARIO" >&2
    ls /opt/sandbox/scenarios >&2
    exit 2
  fi
  echo "[sandbox] scenariusz: $SCENARIO"
  "/opt/sandbox/scenarios/$SCENARIO/break.sh"
fi

nginx
shopctl start || true            # scenariusz moze celowo zepsuc start aplikacji
cd /opt/pipe
exec python -m backend.server
