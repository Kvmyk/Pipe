# Serwer demonstracyjny Pipe (piaskownica)

To kontener udajacy maly VPS ze sklepem internetowym. Nie ma systemd.

- nginx: konfiguracja `/etc/nginx/sites-enabled/shop`, port 80; sprawdzenie `nginx -t`, przeladowanie `nginx -s reload`
- aplikacja sklepu: `/opt/sandbox/app/app.py`, konfiguracja `/etc/shop/app.conf`, log `/var/log/shop/app.log`,
  sterowanie `shopctl start|stop|restart|status`; nginx przekazuje do niej ruch (`proxy_pass`)
- pliki statyczne: `/var/www/shop/static/`, serwowane przez nginx pod `/static/`
- sprawdzenie, czy sklep dziala: `curl -s localhost/` (powinno zwrocic "Sklep dziala")
