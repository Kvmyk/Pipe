---
name: nginx-vhost
description: Nowa strona (domena) w nginx jako reverse proxy do aplikacji, z certyfikatem Let's Encrypt (certbot).
---

# Nowa domena w nginx + certyfikat

Zbierz od uzytkownika: domena (np. app.example.com), dokad kierowac ruch (port aplikacji, np. 127.0.0.1:3000),
e-mail do Let's Encrypt. Nie zgaduj — dopytaj.

## 1. Sprawdz warunki (tylko odczyty)
- DNS: server_history operation=checks albo `getent ahosts DOMENA` — domena musi wskazywac na ten serwer.
- Czy aplikacja odpowiada lokalnie: `curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:PORT/`.
- Gdzie dziala nginx: na hoscie (`ls /etc/nginx/sites-enabled`) czy w kontenerze (docker_manage ps).
- Czy porty 80 i 443 sa publiczne (network_info ports).
- W trybie docker (SRODOWISKO) /etc hosta jest tylko do odczytu — kroki 2-4 podaj uzytkownikowi
  jako komendy do wykonania w powloce serwera, zamiast uruchamiac je sam.

## 2. Konfiguracja nginx
Zapisz `/etc/nginx/sites-available/DOMENA` (write_file — bezpiecznik zrobi kopie i `nginx -t`):

```
server {
    listen 80;
    listen [::]:80;
    server_name DOMENA;

    location / {
        proxy_pass http://127.0.0.1:PORT;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Wlacz: `ln -s /etc/nginx/sites-available/DOMENA /etc/nginx/sites-enabled/DOMENA && systemctl reload nginx`
(bezpiecznik sprawdzi `nginx -t` przed przeladowaniem).

## 3. Certyfikat
`certbot --nginx -d DOMENA --non-interactive --agree-tos -m EMAIL --redirect`
Brak certbota: `apt-get install -y certbot python3-certbot-nginx`.

## 4. Weryfikacja
- `curl -sSI https://DOMENA/` — kod 200/301/302, bez bledu certyfikatu.
- server_history operation=checks — certyfikat wazny ~90 dni; odnawianie: `systemctl list-timers | grep certbot`.
- Dopisz domene do SERVER.md (sekcja Domeny i siec) i katalog aplikacji do DIRECTORY.

Cofniecie: /cofnij (usuwa plik vhosta i link, przeladuje nginx) albo `certbot delete --cert-name DOMENA`.
